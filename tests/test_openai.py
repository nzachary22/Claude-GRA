"""OpenAI as the AI service: translation both ways, and the full loop through a stand-in for the API.

No network call is made. FakeOpenAI plays the API: it reads the Responses input
the app sends, hands the latest tool results to the same scripted analyst the
other tests use, and replies in the Responses output format, including
encrypted reasoning items that must be passed back on the next request.
"""

import base64
import json
from types import SimpleNamespace

import pytest

from gra.activity import read_events, render_text, sent_text
from gra.engine import TOOLS, ReportSession
from gra.gate import QueryGate
from gra.llm_openai import OpenAIClient, from_response, to_input, to_tools
from gra.schema import describe
from tests.scripted import ScriptedModel
from tests.test_engine import GENDER_SQL, gender_table

SECRET_REASONING = "ENCRYPTED-REASONING-BLOB"


class FakeOpenAI:
    """Just enough of openai.OpenAI().responses.stream for the reporting loop."""

    def __init__(self, steps, refuse_at=None, incomplete_at=None):
        self.analyst = ScriptedModel(steps)
        self.requests: list[dict] = []
        self.refuse_at, self.incomplete_at = refuse_at, incomplete_at
        self.responses = SimpleNamespace(stream=self._stream)

    def _stream(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs)))  # what would go over the wire
        n = len(self.requests)
        items = kwargs["input"]
        trailing = []
        for item in reversed(items):
            if item.get("type") != "function_call_output":
                break
            trailing.insert(0, {"type": "tool_result", "tool_use_id": item["call_id"], "content": item["output"]})
        msgs = [{"role": "user", "content": trailing or [{"type": "text", "text": "start"}]}]
        reply = self.analyst.respond(kwargs["instructions"], msgs, kwargs["tools"], lambda: False)
        output = [{"type": "reasoning", "id": f"rs_{n}", "summary": [], "encrypted_content": f"{SECRET_REASONING}-{n}"}]
        status, incomplete = "completed", None
        if n == self.refuse_at:
            output.append({"type": "message", "id": f"msg_{n}", "role": "assistant", "status": "completed",
                           "content": [{"type": "refusal", "refusal": "I can't help with that."}]})
        else:
            for b in reply.content:
                if b["type"] == "tool_use":
                    output.append({"type": "function_call", "id": f"fc_{b['id']}", "call_id": f"call_{b['id']}",
                                   "name": b["name"], "arguments": json.dumps(b["input"]), "status": "completed"})
                else:
                    output.append({"type": "message", "id": f"msg_{n}", "role": "assistant", "status": "completed",
                                   "content": [{"type": "output_text", "text": b["text"], "annotations": []}]})
        if n == self.incomplete_at:
            status, incomplete = "incomplete", {"reason": "max_output_tokens"}
        response = {"id": f"resp_{n}", "model": kwargs["model"], "status": status,
                    "incomplete_details": incomplete, "output": output}
        return _Stream(response)


class _Stream:
    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        yield {"type": "response.created"}

    def get_final_response(self):
        return self.response


def session_with(tiny_db, fake, tmp_path, blocks=None):
    db, catalog = tiny_db
    model = OpenAIClient("test-key", client=fake)
    return ReportSession(QueryGate(db, catalog), describe(db, catalog),
                         blocks or [{"type": "text", "text": "Table 1: clients by gender"}],
                         ("2025-01-01", "2025-12-31"), model, activity_path=tmp_path / "activity.jsonl")


# ---------------------------------------------------------------- translation

def test_uploaded_documents_become_openai_inputs():
    pdf = base64.standard_b64encode(b"%PDF-1.4 test").decode()
    png = base64.standard_b64encode(b"\x89PNG test").decode()
    items = to_input([{"role": "user", "content": [
        {"type": "text", "text": "--- Uploaded reporting document: r.pdf ---"},
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf}, "title": "r.pdf"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png}},
    ]}])
    assert items == [{"role": "user", "content": [
        {"type": "input_text", "text": "--- Uploaded reporting document: r.pdf ---"},
        {"type": "input_file", "filename": "r.pdf", "file_data": f"data:application/pdf;base64,{pdf}"},
        {"type": "input_image", "detail": "high", "image_url": f"data:image/png;base64,{png}"},
    ]}]


def test_tool_results_and_refusals_become_function_outputs():
    items = to_input([
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call_1", "name": "run_query", "input": {"sql": "x"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "Refused: no", "is_error": True}]},
    ])
    assert items == [
        {"type": "function_call", "call_id": "call_1", "name": "run_query", "arguments": '{"sql": "x"}'},
        {"type": "function_call_output", "call_id": "call_1", "output": "ERROR: Refused: no"},
    ]


def test_tools_keep_their_schemas():
    out = to_tools(TOOLS)
    assert [t["name"] for t in out] == ["run_query", "note_assumption", "submit_tables"]
    assert all(t["type"] == "function" and t["parameters"] is src["input_schema"] for t, src in zip(out, TOOLS))


def test_stop_reasons():
    def stop(output, incomplete=None):
        return from_response({"output": output, "incomplete_details": incomplete}).stop_reason

    call = {"type": "function_call", "call_id": "c", "name": "run_query", "arguments": "{}"}
    text = {"type": "message", "content": [{"type": "output_text", "text": "hi"}]}
    assert stop([call]) == "tool_use"
    assert stop([text]) == "end_turn"
    assert stop([call], {"reason": "max_output_tokens"}) == "max_tokens"
    assert stop([text], {"reason": "content_filter"}) == "refusal"
    assert stop([{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]) == "refusal"


def test_unreadable_arguments_reach_the_gate_as_an_empty_call():
    r = from_response({"output": [{"type": "function_call", "call_id": "c", "name": "run_query", "arguments": "{bad"}]})
    assert r.content[0]["input"] == {}


# ---------------------------------------------------------------- full loop

def test_full_run_through_openai(tiny_db, tmp_path):
    fake = FakeOpenAI([
        [("run_query", {"sql": GENDER_SQL, "purpose": "Counting clients by gender"})],
        [("note_assumption", {"id": "A1", "kind": "assumption", "text": "Blank gender is Unknown.", "tables": ["T1"]}),
         ("submit_tables", {"tables": [gender_table()]})],
    ])
    session = session_with(tiny_db, fake, tmp_path)
    session.run()
    assert session.status == "done", session.error
    assert [c.display for c in session.tables[0].rows[-1].cells][-1] == "59"
    assert "A1" in session.notes

    first, second = fake.requests
    assert first["store"] is False and first["model"] == "gpt-6.1-sol" and first["reasoning"] == {"effort": "xhigh"}
    assert first["instructions"] == session_instructions()
    # The second request replays the reasoning item and the call before answering it, as the API requires.
    kinds = [i.get("type", i.get("role")) for i in second["input"]]
    assert kinds == ["user", "reasoning", "function_call", "function_call_output"]
    assert second["input"][1]["encrypted_content"] == f"{SECRET_REASONING}-1"
    assert second["input"][2]["call_id"] == second["input"][3]["call_id"]
    assert '"query_id": "Q1"' in second["input"][3]["output"]


def session_instructions():
    from gra.engine import SYSTEM_PROMPT

    return SYSTEM_PROMPT


def test_correction_continues_the_openai_conversation(tiny_db, tmp_path):
    fake = FakeOpenAI([
        [("run_query", {"sql": GENDER_SQL, "purpose": "Counting clients by gender"})],
        [("submit_tables", {"tables": [gender_table()]})],
        [("run_query", {"sql": GENDER_SQL.replace("GROUP BY", "WHERE county = 'North' GROUP BY"), "purpose": "North"})],
        [("submit_tables", {"tables": [gender_table("Q2")]})],
    ])
    session = session_with(tiny_db, fake, tmp_path)
    session.run()
    session.correct("Count only the North county.")
    assert session.status == "done", session.error
    last = fake.requests[-1]["input"]
    assert any(i.get("role") == "user" and "North county" in json.dumps(i) for i in last)
    calls = [i["call_id"] for i in last if i.get("type") == "function_call"]
    outputs = [i["call_id"] for i in last if i.get("type") == "function_call_output"]
    assert calls == outputs and len(calls) == 3  # the fourth call is in the reply to this request


def test_activity_record_names_openai_and_leaves_out_encrypted_reasoning(tiny_db, tmp_path):
    fake = FakeOpenAI([
        [("run_query", {"sql": GENDER_SQL, "purpose": "Counting clients by gender"})],
        [("submit_tables", {"tables": [gender_table()]})],
    ])
    session = session_with(tiny_db, fake, tmp_path)
    session.run()
    raw = (tmp_path / "activity.jsonl").read_text(encoding="utf-8")
    assert SECRET_REASONING not in raw
    events = read_events(tmp_path / "activity.jsonl")
    text = render_text(events, [], 0, "run1")
    assert "AI service: OpenAI (requests sent with storage turned off), requested model gpt-6.1-sol" in text
    assert "your OpenAI account terms" in text and "Anthropic" not in text
    assert "[The AI's internal reasoning step" in text
    # Everything that went to OpenAI is in the record the protected-value check reads.
    results = "".join(sent_text(e) for e in events)
    assert '"query_id": "Q1"' in results
    assert not session.gate.contains_protected(json.dumps(fake.requests))


def test_activity_report_names_openai(tiny_db, tmp_path):
    import io

    from docx import Document

    from gra.activity_report import to_report_docx

    fake = FakeOpenAI([[("submit_tables", {"tables": [gender_table()]})]])
    session = session_with(tiny_db, fake, tmp_path)
    session.run()
    events = read_events(tmp_path / "activity.jsonl")
    data = to_report_docx(events, session.gate.catalog, session.gate.contains_protected,
                          session.gate.protected_value_count())
    text = "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs)
    assert "AI service: OpenAI" in text and "OpenAI account terms" in text


def test_nothing_identifying_reaches_openai(tiny_db, tmp_path):
    fake = FakeOpenAI([
        [("run_query", {"sql": "SELECT client_id, COUNT(*) AS n FROM clients GROUP BY client_id", "purpose": "probe"})],
        [("run_query", {"sql": GENDER_SQL, "purpose": "Counting clients by gender"})],
        [("submit_tables", {"tables": [gender_table("Q2")]})],
    ])
    session = session_with(tiny_db, fake, tmp_path)
    session.run()
    sent = json.dumps(fake.requests)
    assert "C-001" not in sent and "Stone" not in sent and "1980-02-01" not in sent and "mold" not in sent
    refused = [i for i in fake.requests[1]["input"] if i.get("type") == "function_call_output"]
    assert refused[0]["output"].startswith("ERROR: Refused")


def test_refusal_and_cut_off_replies(tiny_db, tmp_path):
    fake = FakeOpenAI([[("run_query", {"sql": GENDER_SQL, "purpose": "x"})]], refuse_at=1)
    session = session_with(tiny_db, fake, tmp_path)
    session.run()
    assert session.status == "failed" and "declined" in session.error

    fake = FakeOpenAI([
        [("run_query", {"sql": GENDER_SQL, "purpose": "x"})],
        [("submit_tables", {"tables": [gender_table("Q1")]})],
    ], incomplete_at=1)
    (tmp_path / "b").mkdir()
    session = session_with(tiny_db, fake, tmp_path / "b")
    session.run()
    out = [i for i in fake.requests[1]["input"] if i.get("type") == "function_call_output"]
    assert "cut off" in out[0]["output"]


def test_cancel_stops_an_openai_request(tiny_db, tmp_path):
    fake = FakeOpenAI([[("run_query", {"sql": GENDER_SQL, "purpose": "x"})]])
    session = session_with(tiny_db, fake, tmp_path)
    session.cancel()
    session._cancel.clear()
    model = session.model
    with pytest.raises(Exception) as e:
        model.respond("s", session.messages, TOOLS, lambda: True)
    assert type(e.value).__name__ == "Cancelled"


# ---------------------------------------------------------------- settings

def test_provider_choice_and_keys(tmp_path, monkeypatch):
    from gra import llm

    monkeypatch.setenv("GRA_DATA_DIR", str(tmp_path))
    store = {}
    monkeypatch.setattr(llm, "_windows_user_env", lambda name: None)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda s, u: store.get(u))
    monkeypatch.setattr(keyring, "set_password", lambda s, u, k: store.__setitem__(u, k))

    assert llm.active_provider() == "anthropic"
    with pytest.raises(RuntimeError, match="No Anthropic Claude API key"):
        llm.make_client()
    llm.save_api_key("sk-test-openai-key-123456", "openai")
    assert llm.active_provider() == "openai"  # the only service with a key
    assert llm.key_status() == {"anthropic": False, "openai": True}
    assert store == {"openai_api_key": "sk-test-openai-key-123456"}
    llm.save_api_key("sk-ant-test-key-1234567890", "anthropic")
    llm.set_provider("anthropic")
    assert llm.active_provider() == "anthropic"
    assert llm.get_api_key("openai") == "sk-test-openai-key-123456"
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    assert llm.get_api_key("openai") == "env-key"
    with pytest.raises(ValueError):
        llm.set_provider("gemini")


def test_openai_problems_in_plain_words():
    import httpx
    import openai

    from gra.llm_openai import problem_message

    req = httpx.Request("POST", "https://api.openai.com/v1/responses")

    def err(cls, code, body):
        return cls(body, response=httpx.Response(code, request=req), body=None)

    assert "out of API credit" in problem_message(err(openai.RateLimitError, 429,
                                                      "Error code: 429 - {'error': {'code': 'insufficient_quota'}}"))
    assert "busy" in problem_message(err(openai.RateLimitError, 429, "Rate limit reached"))
    assert "not accepted" in problem_message(err(openai.AuthenticationError, 401, "bad key"))
    assert problem_message(err(openai.InternalServerError, 500, "oops")) == "The AI service returned an error (500)."


def test_real_openai_library_sends_the_expected_request(tiny_db, tmp_path):
    """The installed OpenAI library accepts the request and its streamed reply parses, with no network."""
    import httpx
    import openai

    seen = []
    response = {"id": "resp_1", "object": "response", "created_at": 0, "model": "gpt-6.1-sol", "status": "completed",
                "output": [{"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "enc"},
                           {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "submit_tables",
                            "arguments": json.dumps({"tables": [gender_table()]}), "status": "completed"}],
                "parallel_tool_calls": True, "tool_choice": "auto", "tools": [], "incomplete_details": None,
                "error": None, "instructions": None, "metadata": {}, "temperature": 1.0, "top_p": 1.0}

    def handler(request):
        seen.append(json.loads(request.content))
        events = [("response.created", {"type": "response.created", "sequence_number": 0,
                                        "response": {**response, "status": "in_progress", "output": []}}),
                  ("response.completed", {"type": "response.completed", "sequence_number": 1, "response": response})]
        body = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    sdk = openai.OpenAI(api_key="test", base_url="https://api.openai.test/v1",
                        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    db, catalog = tiny_db
    gate = QueryGate(db, catalog)
    gate.run(GENDER_SQL, "Counting clients by gender")
    session = ReportSession(gate, describe(db, catalog), [{"type": "text", "text": "Table 1"}],
                            ("2025-01-01", "2025-12-31"), OpenAIClient("test", client=sdk))
    session.run()
    assert session.status == "done", session.error
    body = seen[0]
    assert body["store"] is False and body["stream"] is True and body["max_output_tokens"] == 64000
    assert body["tools"][0]["name"] == "run_query" and body["input"][0]["role"] == "user"
