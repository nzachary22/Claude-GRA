"""Choosing a model other than each service's default."""

import json
from types import SimpleNamespace

from gra import llm, llm_openai
from gra.paths import app_data_dir
from tests.live_eval import estimate_cost, track_usage


def test_default_models_unchanged():
    assert llm.make_client("anthropic").model == llm.MODEL
    assert llm.make_client("openai").model == llm_openai.MODEL


def test_model_from_settings_and_environment(monkeypatch):
    app_data_dir().mkdir(parents=True, exist_ok=True)
    (app_data_dir() / "settings.json").write_text(json.dumps({"models": {"anthropic": "claude-haiku-5-5"}}))
    assert llm.make_client("anthropic").model == "claude-haiku-5-5"
    assert llm.make_client("openai").model == llm_openai.MODEL
    monkeypatch.setenv("GRA_MODEL", "gpt-6-luna")
    assert llm.make_client("openai").model == "gpt-6-luna"
    assert llm.make_client("openai", "gpt-6.1-terra").model == "gpt-6.1-terra"


def test_haiku_request_leaves_out_server_fallback():
    opus = llm.ClaudeClient("k").request("s", [], [])
    assert opus["fallbacks"] == "default" and opus["model"] == "claude-opus-5-5"
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
