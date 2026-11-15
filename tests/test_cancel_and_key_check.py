"""V.2.0.3: Cancel ends a run at once, and a new API key is tried with one tiny test message."""

import threading
import time

import pytest

from gra import llm, llm_web
from gra.engine import Cancelled, run_cancellable


# ---------------------------------------------------------------- Cancel

class SilentStream:
    """An AI reply that sends nothing for a long time, like a model thinking hard."""

    def __init__(self):
        self.closed = threading.Event()

    def close(self):
        self.closed.set()


def test_cancel_ends_a_silent_request_at_once():
    stream = SilentStream()
    cancel = threading.Event()

    def call(handle):
        handle["stream"] = stream
        stream.closed.wait(30)  # blocks until the connection is closed
        return "late answer"

    threading.Timer(0.3, cancel.set).start()
    start = time.monotonic()
    with pytest.raises(Cancelled):
        run_cancellable(call, cancel.is_set, poll=0.05)
    assert time.monotonic() - start < 1.5
    assert stream.closed.is_set()  # the connection to the AI service was closed, so it stops working (and charging)


def test_a_finished_request_returns_its_answer_and_errors_pass_through():
    assert run_cancellable(lambda h: 42, lambda: False) == 42

    def fails(h):
        raise RuntimeError("The AI service declined this request.")

    with pytest.raises(RuntimeError, match="declined"):
        run_cancellable(fails, lambda: False)


def test_claude_client_cancel_does_not_wait_for_the_reply():
    from gra.llm import ClaudeClient

    stream = SilentStream()

    class Manager:
        def __enter__(self):
            return self

        def __exit__(self, *a):  # as the SDK does: leaving the block closes the connection
            stream.close()
            return False

        def __iter__(self):
            stream.closed.wait(30)
            return iter(())

        def close(self):
            stream.close()

    client = ClaudeClient.__new__(ClaudeClient)
    client.model, client.effort = "claude-sonnet-5-5", "high"
    client.client = type("C", (), {"beta": type("B", (), {"messages": type("M", (), {
        "stream": staticmethod(lambda **kw: Manager())})()})()})()
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    start = time.monotonic()
    with pytest.raises(Cancelled):
        client.respond("sys", [{"role": "user", "content": "hi"}], [], cancel.is_set)
    assert time.monotonic() - start < 1.5
    assert stream.closed.wait(5)  # closed at once, or as soon as the connection opens


def test_cancel_while_importing_shows_stopped_at_once(tmp_path, monkeypatch):
    from gra.api import Api, Run

    monkeypatch.setenv("GRA_DATA_DIR", str(tmp_path))
    api = Api(model_factory=lambda: None, spawn=lambda *a: None)  # the import never gets to run
    run = Run("abcdef123456", tmp_path, ("2025-01-01", "2025-12-31"), ["r.pdf"], ["d.csv"])
    api.runs[run.id] = run
    api.cancel(run.id)
    polled = api.poll(run.id)
    assert polled["status"] == "cancelled"
    assert [e["kind"] for e in polled["events"]].count("cancelled") == 1
    api.cancel(run.id)  # pressing it again adds nothing
    assert [e["kind"] for e in api.poll(run.id)["events"]].count("cancelled") == 1


# ---------------------------------------------------------------- key check

@pytest.fixture
def browser(monkeypatch):
    """Answer the key check's requests the way the AI service would; records what was sent."""
    monkeypatch.setattr(llm, "in_browser", lambda: True)
    monkeypatch.setattr(llm_web.time, "sleep", lambda s: None)
    sent, replies = [], []

    def transport(method, url, headers, body):
        sent.append((method, url))
        return replies.pop(0) if len(replies) > 1 else replies[0]

    monkeypatch.setattr(llm_web, "transport", transport)
    return sent, replies


@pytest.mark.parametrize("provider,reply,expect", [
    ("anthropic", (400, '{"error": {"type": "invalid_request_error", "message": "Your credit balance is too low"}}'),
     "no API credit left. Add credit at console.anthropic.com"),
    ("openai", (429, '{"error": {"code": "insufficient_quota", "message": "You exceeded your current quota"}}'),
     "no API credit left. Add credit at platform.openai.com"),
    ("openai", (403, '{"error": {"message": "Your organization must be verified to use the model"}}'),
     "verify your organization"),
    ("anthropic", (403, '{"error": {"type": "permission_error"}}'), "does not have permission"),
    ("anthropic", (401, '{"error": {"type": "authentication_error"}}'), "was not accepted"),
    ("openai", (0, ""), "Could not reach the AI service"),
])
def test_key_problems_are_explained_and_the_key_is_not_saved(browser, provider, reply, expect):
    sent, replies = browser
    replies.append(reply)
    problem, note = llm.check_api_key("k" * 30, provider)
    assert expect in problem and note is None


def test_a_busy_service_still_saves_the_key_with_a_note(browser):
    sent, replies = browser
    replies.append((529, '{"error": {"type": "overloaded_error"}}'))
    problem, note = llm.check_api_key("k" * 30, "anthropic")
    assert problem is None and "too busy" in note


def test_a_refused_test_message_falls_back_to_the_free_lookup(browser):
    """If the service ever rejects the test message itself, a working key is still accepted."""
    sent, replies = browser
    replies.extend([(400, '{"error": {"message": "max_output_tokens is too small"}}'), (200, "{}")])
    assert llm.check_api_key("k" * 30, "openai") == (None, None)
    assert sent == [("POST", "https://api.openai.com/v1/responses"),
                    ("GET", "https://api.openai.com/v1/models/gpt-6.1-sol")]


def test_desktop_key_check_sends_one_tiny_message(monkeypatch):
    import anthropic

    monkeypatch.setattr(llm, "in_browser", lambda: False)
    calls = []

    class Messages:
        def create(self, **kw):
            calls.append(kw)

    class Fake:
        def __init__(self, **kw):
            self.messages = Messages()

    monkeypatch.setattr(anthropic, "Anthropic", Fake)
    assert llm.check_api_key(" " + "k" * 30 + " ", "anthropic") == (None, None)
    assert calls[0]["max_tokens"] == 4 and calls[0]["model"] == llm.MODEL


def test_saving_a_key_returns_the_note(tmp_path, monkeypatch):
    from gra.api import Api

    monkeypatch.setattr(llm, "check_api_key", lambda key, provider: (None, "busy note"))
    monkeypatch.setattr(llm, "save_api_key", lambda key, provider: None)
    monkeypatch.setattr(llm, "set_provider", lambda provider: None)
    assert Api().set_key("k" * 30, "openai") == {"ok": True, "note": "busy note"}


def test_in_the_browser_the_request_runs_directly(monkeypatch):
    """Pyodide has no helper threads; there Cancel stops the whole worker instead."""
    import sys

    monkeypatch.setattr(sys, "platform", "emscripten")
    monkeypatch.setattr(threading, "Thread", None)  # any attempt to start one would fail
    assert run_cancellable(lambda h: "answer", lambda: False) == "answer"
