"""Each service's one model, and the overrides used only by tests and live evaluations."""

import json
from types import SimpleNamespace

from gra import llm, llm_openai
from gra.paths import app_data_dir
from tests.live_eval import estimate_cost, track_usage


def test_one_model_per_service():
    claude, openai = llm.make_client("anthropic"), llm.make_client("openai")
    assert (claude.model, claude.effort) == ("claude-sonnet-5-5", "high")
    assert (openai.model, openai.effort) == ("gpt-6.1-sol", "xhigh")


def test_settings_file_cannot_change_the_model(monkeypatch):
    app_data_dir().mkdir(parents=True, exist_ok=True)
    (app_data_dir() / "settings.json").write_text(json.dumps({"models": {"anthropic": "claude-haiku-5-5"}}))
    assert llm.make_client("anthropic").model == "claude-sonnet-5-5"
    monkeypatch.setenv("GRA_MODEL", "gpt-6-luna")
    assert llm.make_client("openai").model == "gpt-6-luna"
    assert llm.make_client("openai", "gpt-6.1-terra").model == "gpt-6.1-terra"


def test_haiku_request_leaves_out_server_fallback():
    sonnet = llm.ClaudeClient("k").request("s", [], [])
    assert sonnet["fallbacks"] == "default" and sonnet["model"] == "claude-sonnet-5-5"
    haiku = llm.ClaudeClient("k", "claude-haiku-5-5").request("s", [], [])
    assert "fallbacks" not in haiku and "betas" not in haiku
    assert haiku["thinking"] == {"type": "adaptive"} and haiku["output_config"] == {"effort": "high"}


def test_openai_usage_and_cost_estimate():
    resp = {"output": [], "model": "gpt-6-luna",
            "usage": {"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 400}, "output_tokens": 200}}
    out = llm_openai.from_response(resp)
    assert out.usage == {"input_tokens": 600, "cache_read_input_tokens": 400, "cache_creation_input_tokens": 0,
                         "output_tokens": 200}
    model = SimpleNamespace(model="gpt-6-luna", respond=lambda *a: out)
    track_usage(model)
    model.respond("s", [], [], None)
    cost = estimate_cost("gpt-6-luna", model.usage_log)
    assert cost["requests"] == 1 and cost["usd"] == round((600 * 0.10 + 400 * 0.01 + 200 * 0.50) / 1e6, 4)


def test_haiku_long_prompts_cost_more():
    short = [{"input_tokens": 90_000, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 0}]
    long = [{"input_tokens": 110_000, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 0}]
    assert estimate_cost("claude-haiku-5-5", short)["usd"] == 0.009
    assert estimate_cost("claude-haiku-5-5", long)["usd"] == 0.055


def test_effort_override(monkeypatch):
    assert llm.make_client("openai", "gpt-6-luna", "max").effort == "max"
    monkeypatch.setenv("GRA_EFFORT", "xhigh")
    assert llm.make_client("anthropic").effort == "xhigh"


def test_step_up_reasons():
    from gra.engine import Note
    from tests.stepup import trouble
    from gra.tables import Cell, Row, Table

    def table(total):
        rows = [Row("A", "detail", 0, [Cell(3, source="Q1")]), Row("B", "detail", 0, [Cell(4, source="Q1")]),
                Row("Total", "total", 0, [Cell(total, source="Q2")])]
        return Table("t1", "Table 1", "", ["Clients"], rows)

    ok = SimpleNamespace(status="done", error=None, tables=[table(6)], notes={}, events=[])
    assert trouble(ok) == []  # an unduplicated total below the sum of rows is normal
    bad = SimpleNamespace(status="done", error=None, tables=[table(9)], events=[SimpleNamespace(kind="refused")] * 6,
                          notes={"n1": Note("n1", "warning", "The matrix is not included in full.", [])})
    reasons = trouble(bad)
    assert len(reasons) == 3 and "add to only 7" in reasons[0]
    failed = SimpleNamespace(status="failed", error="limit reached", tables=[], notes={}, events=[])
    assert trouble(failed)[:2] == ["The run ended as failed: limit reached", "No tables were produced."]
