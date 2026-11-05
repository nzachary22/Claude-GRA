"""Each AI service has one fixed model and reasoning effort; nothing a user can set changes them."""

import json
from types import SimpleNamespace

from gra import llm, llm_openai
from gra.paths import app_data_dir
from tests.live_eval import estimate_cost, track_usage


def test_fixed_model_for_each_service():
    claude, openai_client = llm.make_client("anthropic"), llm.make_client("openai")
    assert (claude.model, claude.effort) == ("claude-sonnet-5-5", "high")
    assert (openai_client.model, openai_client.effort) == ("gpt-6.1-sol", "xhigh")


def test_settings_and_environment_cannot_change_the_model(monkeypatch):
    app_data_dir().mkdir(parents=True, exist_ok=True)
    (app_data_dir() / "settings.json").write_text(json.dumps({"models": {"anthropic": "x", "openai": "x"}}))
    monkeypatch.setenv("GRA_MODEL", "x")
    monkeypatch.setenv("GRA_EFFORT", "x")
    for provider, model, effort in (("anthropic", "claude-sonnet-5-5", "high"), ("openai", "gpt-6.1-sol", "xhigh")):
        client = llm.make_client(provider)
        assert (client.model, client.effort) == (model, effort)


def test_claude_request_settings():
    params = llm.ClaudeClient("k").request("s", [], [])
    assert params["model"] == "claude-sonnet-5-5" and params["fallbacks"] == "default"
    assert params["thinking"] == {"type": "adaptive"} and params["output_config"] == {"effort": "high"}


def test_openai_usage_and_cost_estimate():
    resp = {"output": [], "model": "gpt-6.1-sol",
            "usage": {"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 400}, "output_tokens": 200}}
    out = llm_openai.from_response(resp)
    assert out.usage == {"input_tokens": 600, "cache_read_input_tokens": 400, "cache_creation_input_tokens": 0,
                         "output_tokens": 200}
    model = SimpleNamespace(model="gpt-6.1-sol", respond=lambda *a: out)
    track_usage(model)
    model.respond("s", [], [], None)
    cost = estimate_cost("gpt-6.1-sol", model.usage_log)
    assert cost["requests"] == 1 and cost["usd"] == round((600 * 2.00 + 400 * 0.20 + 200 * 10.00) / 1e6, 4)
