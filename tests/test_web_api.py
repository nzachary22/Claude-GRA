"""The browser version's request layer and AI connections, tested without a browser."""

import json

import pytest

from gra import llm_web


def test_streamed_claude_reply_is_put_back_together():
    events = [
        ("message_start", {"type": "message_start", "message": {"id": "m", "model": "claude-sonnet-5-5", "content": [],
                                                                "usage": {"input_tokens": 5}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Plan "}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}}),
        ("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "t1", "name": "run_query", "input": {}}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"sql": "SEL'}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": 'ECT 1"}'}}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 9}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    text = "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events)
    m = llm_web.assemble_message(text)
    assert m["stop_reason"] == "tool_use"
    assert m["content"][0] == {"type": "thinking", "thinking": "Plan ", "signature": "sig"}
    assert m["content"][1]["input"] == {"sql": "SELECT 1"}
    assert m["usage"] == {"input_tokens": 5, "output_tokens": 9}


def test_a_cut_off_reply_is_an_error():
    with pytest.raises(RuntimeError, match="cut off"):
        llm_web.assemble_message('event: message_start\ndata: {"type": "message_start", "message": {}}\n\n')


def test_claude_request_matches_the_desktop_request(monkeypatch):
    seen = {}

    def transport(method, url, headers, body):
        seen.update(method=method, url=url, headers=headers, body=json.loads(body))
        return 200, ('event: message_start\ndata: {"type": "message_start", "message": {"model": "x"}}\n\n'
                     'event: message_delta\ndata: {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}\n\n')

    monkeypatch.setattr(llm_web, "transport", transport)
    from gra.llm import ClaudeClient, EFFORT, MODEL

    c = llm_web.BrowserClaudeClient("k" * 30, MODEL, EFFORT)
    r = c.respond("sys", [{"role": "user", "content": "hi"}], [], lambda: False)
    assert r.stop_reason == "end_turn"
    desktop = ClaudeClient.request(c, "sys", [{"role": "user", "content": "hi"}], [])
    betas = desktop.pop("betas", None)
    assert seen["body"] == {**desktop, "stream": True}
    assert seen["headers"]["anthropic-dangerous-direct-browser-access"] == "true"
    assert (seen["headers"].get("anthropic-beta") or None) == (",".join(betas) if betas else None)


@pytest.mark.parametrize("status,text,expected", [
    (401, "{}", "not accepted"),
    (429, "{}", "busy"),
    (0, "", "Could not reach"),
    (400, '{"error": {"type": "invalid_request_error", "message": "Your credit balance is too low"}}', "out of API credit"),
])
def test_claude_problems_in_plain_words(monkeypatch, status, text, expected):
    monkeypatch.setattr(llm_web, "transport", lambda *a: (status, text))
    monkeypatch.setattr(llm_web.time, "sleep", lambda s: None)
    c = llm_web.BrowserClaudeClient("k" * 30, "claude-sonnet-5-5", "high")
    with pytest.raises(RuntimeError, match=expected):
        c.respond("s", [{"role": "user", "content": "x"}], [], lambda: False)


def test_busy_service_is_retried(monkeypatch):
    replies = [(529, "{}"), (503, "{}"), (200, 'event: message_delta\ndata: {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}\n\n')]
    monkeypatch.setattr(llm_web, "transport", lambda *a: replies.pop(0))
    monkeypatch.setattr(llm_web.time, "sleep", lambda s: None)
    c = llm_web.BrowserClaudeClient("k" * 30, "claude-sonnet-5-5", "high")
    assert c.respond("s", [{"role": "user", "content": "x"}], [], lambda: False).stop_reason == "end_turn"


def test_openai_reply_comes_from_the_completed_event(monkeypatch):
    final = {"id": "r", "status": "completed", "model": "gpt-6.1-sol", "output": [
        {"type": "function_call", "call_id": "c1", "name": "run_query", "arguments": '{"sql": "SELECT 1"}'}],
        "usage": {"input_tokens": 3, "output_tokens": 4}}
    seen = {}

    def transport(method, url, headers, body):
        seen.update(url=url, body=json.loads(body), headers=headers)
        return 200, f'event: response.completed\ndata: {json.dumps({"type": "response.completed", "response": final})}\n\n'

    monkeypatch.setattr(llm_web, "transport", transport)
    from gra.llm_openai import OpenAIClient

    client = OpenAIClient("k" * 30, "gpt-6.1-sol", "xhigh", client=llm_web.BrowserOpenAI("k" * 30))
    r = client.respond("sys", [{"role": "user", "content": "hi"}], [], lambda: False)
    assert r.stop_reason == "tool_use" and r.content[0]["input"] == {"sql": "SELECT 1"}
    assert seen["url"].endswith("/v1/responses") and seen["body"]["store"] is False and seen["body"]["stream"] is True
    assert seen["headers"]["authorization"] == "Bearer " + "k" * 30


def test_openai_problems_reach_the_user_unchanged(monkeypatch):
    monkeypatch.setattr(llm_web, "transport", lambda *a: (429, '{"error": {"code": "insufficient_quota"}}'))
    monkeypatch.setattr(llm_web.time, "sleep", lambda s: None)
    from gra.llm_openai import OpenAIClient

    client = OpenAIClient("k" * 30, "gpt-6.1-sol", "xhigh", client=llm_web.BrowserOpenAI("k" * 30))
    with pytest.raises(RuntimeError, match="out of API credit"):
        client.respond("sys", [{"role": "user", "content": "hi"}], [], lambda: False)


def test_key_check(monkeypatch):
    monkeypatch.setattr(llm_web, "transport", lambda *a: (401, "{}"))
    assert llm_web.check_api_key("k" * 30, "anthropic") == "That API key was not accepted."
    monkeypatch.setattr(llm_web, "transport", lambda *a: (200, "{}"))
    assert llm_web.check_api_key("k" * 30, "openai") is None


def test_browser_keys_are_kept_in_the_app_folder(monkeypatch):
    from gra import llm

    monkeypatch.setattr(llm, "in_browser", lambda: True)
    assert llm.get_api_key("anthropic") is None
    llm.save_api_key("a" * 30, "anthropic")
    assert llm.get_api_key("anthropic") == "a" * 30 and llm.key_status() == {"anthropic": True, "openai": False}
    llm.delete_api_key("anthropic")
    assert llm.get_api_key("anthropic") is None


def test_requests_are_routed_to_the_shared_api(monkeypatch):
    from gra import web_api

    monkeypatch.setattr(web_api.API, "demo_pause", 0)
    pushed = []
    web_api.set_publisher(lambda run, reply: pushed.append((run, reply["status"])))
    try:
        r = web_api.handle("POST", "/api/demo")
        run = r["json"]["id"]
        assert web_api.has_jobs() and web_api.job_run_id() == run
        web_api.run_jobs()
        assert pushed and pushed[-1] == (run, "done")
        poll = web_api.handle("GET", f"/api/runs/{run}?since=2")
        assert poll["json"]["status"] == "done" and poll["json"]["event_count"] >= 2
        data, media, name = web_api.handle("GET", f"/api/runs/{run}/export.xlsx")["file"]
        assert name.endswith(".xlsx") and len(data) > 5000
        assert web_api.handle("GET", "/api/runs/nope/export.xlsx")["status"] == 404
        assert web_api.handle("GET", "/api/nothing")["status"] == 404
        sample = web_api.handle("GET", "/api/sample/Juniper%20Ridge%202025%20annual%20report.pdf")["file"]
        assert sample[1] == "application/pdf"
        assert web_api.handle("POST", "/api/key", json.dumps({"key": "short"}))["status"] == 400
    finally:
        web_api.set_publisher(None)


def test_stopped_run_gets_its_ai_record_back(monkeypatch):
    from gra import web_api
    from gra.paths import app_data_dir

    monkeypatch.setattr(web_api.API, "demo_pause", 0)
    lines = []
    web_api.set_activity_hook(lambda run, line: lines.append(line))
    try:
        run = web_api.handle("POST", "/api/demo")["json"]["id"]
        web_api.run_job()  # the preparation step only
        web_api.run_job()  # and the work with the (recorded) AI
        path = app_data_dir() / "runs" / run / "activity.jsonl"
        saved = path.read_text().splitlines()
        path.write_text("\n".join(saved[:2]) + "\n")  # as if the stop came before the rest was saved
        r = web_api.handle("POST", f"/api/web/restore-activity/{run}", json.dumps({"lines": lines}))
        assert r["status"] == 200
        events = [json.loads(x) for x in path.read_text().splitlines()]
        assert [e["seq"] for e in events[:-1]] == list(range(1, len(saved) + 1))
        assert events[-1]["kind"] == "cancelled"
        assert web_api.handle("GET", f"/api/runs/{run}/activity-report")["status"] == 200
    finally:
        web_api.set_activity_hook(None)
