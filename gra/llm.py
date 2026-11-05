"""AI service choice, API key storage and the Claude client.

The user picks Anthropic (Claude) or OpenAI and enters their own API key once.
Keys are kept in the operating system's credential store (Windows Credential
Manager or the Mac Keychain), never in the repository; the choice of service is kept in the local
app-data folder. The OpenAI client lives in llm_openai.py.

The AI SDKs can take close to a minute to import on a slow or busy computer,
so they load only when needed and the page never waits on them.
"""

from __future__ import annotations

import json
import os
from typing import Callable

from .engine import Cancelled
from .paths import app_data_dir

MODEL = "claude-opus-5-5"
_KEYRING_SERVICE = "GrantReportingAssistant"
PROVIDERS = {
    "anthropic": {"label": "Anthropic Claude", "env": "ANTHROPIC_API_KEY", "keyring_user": "anthropic_api_key",
                  "terms": "Anthropic"},
    "openai": {"label": "OpenAI", "env": "OPENAI_API_KEY", "keyring_user": "openai_api_key", "terms": "OpenAI"},
}
DEFAULT_PROVIDER = "anthropic"


def _check_provider(provider: str) -> str:
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown AI service: {provider}")
    return provider


def get_api_key(provider: str | None = None) -> str | None:
    p = PROVIDERS[_check_provider(provider or active_provider())]
    env = os.environ.get(p["env"]) or _windows_user_env(p["env"])
    if env:
        return env
    try:
        import keyring

        return keyring.get_password(_KEYRING_SERVICE, p["keyring_user"])
    except Exception:
        return None


def _settings_path():
    return app_data_dir() / "settings.json"


def _read_settings() -> dict:
    try:
        settings = json.loads(_settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return settings if isinstance(settings, dict) else {}


def _write_setting(name: str, value) -> None:
    settings = _read_settings()
    settings[name] = value
    _settings_path().write_text(json.dumps(settings), encoding="utf-8")


def saved_provider() -> str | None:
    value = _read_settings().get("provider")
    return value if value in PROVIDERS else None


def set_provider(provider: str) -> None:
    _write_setting("provider", _check_provider(provider))


def settings_seen() -> bool:
    """Whether Settings has been shown since the AI service choice arrived (V.1.1)."""
    return bool(_read_settings().get("settings_seen"))


def mark_settings_seen() -> None:
    _write_setting("settings_seen", True)


def active_provider() -> str:
    """The chosen service; before any choice, the one with a key (Anthropic if both or neither)."""
    chosen = saved_provider()
    if chosen:
        return chosen
    if not _has_key(DEFAULT_PROVIDER) and _has_key("openai"):
        return "openai"
    return DEFAULT_PROVIDER


def _has_key(provider: str) -> bool:
    return bool(get_api_key(provider))


def key_status() -> dict:
    return {name: _has_key(name) for name in PROVIDERS}


def make_client(provider: str | None = None):
    """A model client for the chosen service, or a plain-language error if no key is set up."""
    provider = _check_provider(provider or active_provider())
    key = get_api_key(provider)
    if not key:
        raise RuntimeError(f"No {PROVIDERS[provider]['label']} API key is set up yet. Add it under Settings.")
    if provider == "openai":
        from .llm_openai import OpenAIClient

        return OpenAIClient(key)
    return ClaudeClient(key)


def _windows_user_env(name: str) -> str | None:
    """A user-level variable set after this process started (Windows only)."""
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return winreg.QueryValueEx(k, name)[0] or None
    except OSError:
        return None


def save_api_key(key: str, provider: str = DEFAULT_PROVIDER) -> None:
    import keyring

    keyring.set_password(_KEYRING_SERVICE, PROVIDERS[_check_provider(provider)]["keyring_user"], key.strip())


def check_api_key(key: str, provider: str = DEFAULT_PROVIDER) -> str | None:
    """Return None if the key works, else a plain-language problem."""
    if _check_provider(provider) == "openai":
        from .llm_openai import check_api_key as check_openai

        return check_openai(key)
    import anthropic

    try:
        anthropic.Anthropic(api_key=key.strip(), max_retries=1).models.retrieve(MODEL)
        return None
    except anthropic.AuthenticationError:
        return "That API key was not accepted."
    except anthropic.PermissionDeniedError:
        return "That API key does not have access to the model."
    except anthropic.APIConnectionError:
        return "Could not reach the AI service. Check the internet connection."
    except anthropic.APIStatusError as e:
        return f"The AI service returned an error ({e.status_code})."


def status_message(code: int, detail: str) -> str:
    if "credit balance" in detail.lower():
        return ("Your Anthropic account is out of API credit. Add credit at console.anthropic.com "
                "under Plans & Billing, then start the report again.")
    return f"The AI service returned an error ({code})."


class ClaudeClient:
    provider = "anthropic"

    def __init__(self, api_key: str, model: str = MODEL, effort: str = "high"):
        import anthropic

        self.client = anthropic.Anthropic(api_key=api_key, max_retries=4)
        self.model = model
        self.effort = effort

    def respond(self, system: str, messages: list, tools: list, should_cancel: Callable[[], bool]):
        import anthropic

        try:
            with self.client.beta.messages.stream(
                model=self.model,
                max_tokens=64000,
                system=system,
                messages=messages,
                tools=tools,
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
                cache_control={"type": "ephemeral"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            ) as stream:
                for _ in stream:
                    if should_cancel():
                        raise Cancelled()
                return stream.get_final_message()
        except anthropic.AuthenticationError:
            raise RuntimeError("The saved API key was not accepted. Enter a new key in Settings.") from None
        except anthropic.RateLimitError:
            raise RuntimeError("The AI service is busy (rate limit). Try again in a minute.") from None
        except anthropic.APIConnectionError:
            raise RuntimeError("Could not reach the AI service. Check the internet connection.") from None
        except anthropic.APIStatusError as e:
            raise RuntimeError(status_message(e.status_code, str(e))) from None
