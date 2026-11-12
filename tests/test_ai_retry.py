"""A passing AI service problem (overloaded, server error, a reply cut off part-way) no longer stops a run:
the loop waits and sends the same request again, a few times, before giving up with a plain message."""

import json
from types import SimpleNamespace

import anthropic
import httpx
import httpx2
import openai
import pytest

from gra import llm_web
from gra.engine import Limits, ReportSession, TemporaryAIError
from gra.gate import QueryGate
from gra.llm import ClaudeClient
from gra.llm_openai import OpenAIClient
from gra.schema import describe
from tests.scripted import ScriptedModel
from tests.test_engine import GENDER_SQL, gender_table

NO_WAIT = Limits(retry_waits=(0, 0, 0))
STEPS = [[("run_query", {"sql": GENDER_SQL, "purpose": "Counting clients by gender"})],
         [("submit_tables", {"tables": [gender_table()]})]]


class Flaky:
    """Fails with a temporary problem on the chosen calls, otherwise answers like the scripted model."""

    def __init__(self, fail_on):
        self.inner = ScriptedModel(STEPS)
        self.fail_on = set(fail_on)
        self.calls = 0

    def respond(self, *args):
        self.calls += 1
        if self.calls in self.fail_on:
            raise TemporaryAIError("The AI service is overloaded right now.")
        return self.inner.respond(*args)


def session_for(tiny_db, model, limits=NO_WAIT):
    db, catalog = tiny_db
    return ReportSession(QueryGate(db, catalog), describe(db, catalog), [{"type": "text", "text": "Report"}],
                         ("2025-01-01", "2025-12-31"), model, limits)


def test_run_survives_temporary_problems(tiny_db):
    model = Flaky(fail_on={1, 3, 4})
    s = session_for(tiny_db, model)
    s.run()
    assert s.status == "done", s.error
    assert model.calls == 5
    retries = [e.message for e in s.events if "Trying again" in e.message]
    assert len(retries) == 3 and "overloaded" in retries[0] and "try 2 of 4" in retries[0]
    assert [e["attempt"] for e in s.activity.events if e["kind"] == "service_problem"] == [1, 1, 2]


def test_run_stops_with_a_plain_message_when_the_problem_persists(tiny_db):
    model = Flaky(fail_on=range(1, 100))
    s = session_for(tiny_db, model)
    s.run()
    assert s.status == "failed" and model.calls == 4
    assert "overloaded" in s.error and "after 4 tries" in s.error and "start the report again" in s.error


def test_cancel_while_waiting_to_try_again(tiny_db):
    model = Flaky(fail_on=range(1, 100))
    s = session_for(tiny_db, model, Limits(retry_waits=(30,)))
    s._emit = lambda kind, message: s.cancel() if "Trying again" in message else None
    s.run()
    assert s.status == "cancelled" and model.calls == 1


def test_other_problems_are_not_retried(tiny_db):
    class Broken:
        calls = 0

        def respond(self, *a):
            self.calls += 1
            raise RuntimeError("The saved API key was not accepted. Enter a new key in Settings.")

    model = Broken()
    s = session_for(tiny_db, model)
    s.run()
    assert s.status == "failed" and model.calls == 1 and "not accepted" in s.error


# ---------------------------------------------------------------- browser version

def _browser_claude(monkeypatch, reply):
    monkeypatch.setattr(llm_web, "transport", lambda *a: reply)
    monkeypatch.setattr(llm_web.time, "sleep", lambda s: None)
    return llm_web.BrowserClaudeClient("k" * 30, "claude-sonnet-5-5", "high")


@pytest.mark.parametrize("reply,expected", [
    ((200, 'event: message_start\ndata: {"type": "message_start", "message": {}}\n\n'
           'event: error\ndata: {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}\n\n'),
     "overloaded"),
    ((200, 'event: message_start\ndata: {"type": "message_start", "message": {}}\n\n'), "cut off"),
    ((500, '{"error": {"type": "api_error", "message": "Internal server error"}}'), r"temporary problem \(500\)"),
    ((529, '{"error": {"type": "overloaded_error"}}'), "overloaded"),
    ((0, ""), "Could not reach"),
])
def test_browser_claude_temporary_problems(monkeypatch, reply, expected):
    c = _browser_claude(monkeypatch, reply)
    with pytest.raises(TemporaryAIError, match=expected):
        c.respond("s", [{"role": "user", "content": "x"}], [], lambda: False)


@pytest.mark.parametrize("reply", [
    (401, "{}"),
    (400, '{"error": {"type": "invalid_request_error", "message": "Your credit balance is too low"}}'),
    (400, '{"error": {"type": "invalid_request_error", "message": "prompt is too long"}}'),
])
def test_browser_claude_lasting_problems_are_not_temporary(monkeypatch, reply):
    c = _browser_claude(monkeypatch, reply)
    with pytest.raises(RuntimeError) as e:
        c.respond("s", [{"role": "user", "content": "x"}], [], lambda: False)
    assert not isinstance(e.value, TemporaryAIError)


def test_browser_openai_error_part_way_is_temporary(monkeypatch):
    body = 'event: error\ndata: {"type": "error", "code": "server_error", "message": "An error occurred"}\n\n'
    monkeypatch.setattr(llm_web, "transport", lambda *a: (200, body))
    c = OpenAIClient("k", client=llm_web.BrowserOpenAI("k"))
    with pytest.raises(TemporaryAIError):
        c.respond("s", [{"role": "user", "content": "x"}], [], lambda: False)


def test_browser_openai_out_of_credit_is_not_temporary(monkeypatch):
    monkeypatch.setattr(llm_web, "transport", lambda *a: (429, '{"error": {"code": "insufficient_quota"}}'))
    monkeypatch.setattr(llm_web.time, "sleep", lambda s: None)
    c = OpenAIClient("k", client=llm_web.BrowserOpenAI("k"))
    with pytest.raises(RuntimeError, match="out of API credit") as e:
        c.respond("s", [{"role": "user", "content": "x"}], [], lambda: False)
    assert not isinstance(e.value, TemporaryAIError)


# ---------------------------------------------------------------- desktop version (real SDKs, fake network)

def _sse(events):
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)


def _claude(handler):
    c = ClaudeClient.__new__(ClaudeClient)
    c.client = anthropic.Anthropic(api_key="test", base_url="https://api.anthropic.test", max_retries=0,
                                   http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    c.model, c.effort = "claude-sonnet-5-5", "high"
    return c


def test_desktop_claude_overloaded_part_way_is_temporary():
    def handler(request):
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, text=_sse([
            ("message_start", {"type": "message_start", "message": {
                "id": "m", "type": "message", "role": "assistant", "model": "x", "content": [],
                "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 0}}}),
            ("error", {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})]))

    with pytest.raises(TemporaryAIError, match="overloaded"):
        _claude(handler).respond("s", [{"role": "user", "content": "x"}], [], lambda: False)


@pytest.mark.parametrize("status,temporary", [(500, True), (529, True), (503, True), (400, False), (401, False)])
def test_desktop_claude_status_errors(status, temporary):
    def handler(request):
        return httpx2.Response(status, json={"type": "error", "error": {"type": "x", "message": "y"}})

    with pytest.raises(RuntimeError) as e:
        _claude(handler).respond("s", [{"role": "user", "content": "x"}], [], lambda: False)
    assert isinstance(e.value, TemporaryAIError) == temporary


def test_desktop_claude_dropped_connection_is_temporary():
    def handler(request):
        raise httpx2.RemoteProtocolError("peer closed connection")

    with pytest.raises(TemporaryAIError):
        _claude(handler).respond("s", [{"role": "user", "content": "x"}], [], lambda: False)


def _openai(handler):
    sdk = openai.OpenAI(api_key="test", base_url="https://api.openai.test/v1", max_retries=0,
                        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return OpenAIClient("test", client=sdk)


def test_desktop_openai_error_part_way_is_temporary():
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=_sse([
            ("error", {"type": "error", "sequence_number": 0, "code": "server_error", "message": "An error occurred",
                       "param": None, "error": {"type": "server_error", "message": "An error occurred"}})]))

    with pytest.raises(TemporaryAIError):
        _openai(handler).respond("s", [{"role": "user", "content": "x"}], [], lambda: False)


def test_desktop_openai_stream_ending_early_is_temporary():
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="")

    with pytest.raises(TemporaryAIError):
        _openai(handler).respond("s", [{"role": "user", "content": "x"}], [], lambda: False)


@pytest.mark.parametrize("status,body,temporary", [
    (500, {"error": {"message": "server", "type": "server_error"}}, True),
    (429, {"error": {"message": "You exceeded your current quota", "code": "insufficient_quota"}}, False),
    (400, {"error": {"message": "bad", "type": "invalid_request_error"}}, False),
])
def test_desktop_openai_status_errors(status, body, temporary):
    def handler(request):
        return httpx.Response(status, json=body)

    with pytest.raises(RuntimeError) as e:
        _openai(handler).respond("s", [{"role": "user", "content": "x"}], [], lambda: False)
    assert isinstance(e.value, TemporaryAIError) == temporary


def test_openai_failed_response_on_its_side_is_temporary():
    class Sdk:
        def __init__(self):
            self.responses = self

        def stream(self, **params):
            class S:
                def __enter__(s):
                    return s

                def __exit__(s, *a):
                    return False

                def __iter__(s):
                    return iter(())

                def get_final_response(s):
                    return SimpleNamespace(status="failed", error=SimpleNamespace(code="server_error"))
            return S()

    with pytest.raises(TemporaryAIError):
        OpenAIClient("k", client=Sdk()).respond("s", [{"role": "user", "content": "x"}], [], lambda: False)
