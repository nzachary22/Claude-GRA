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
import sys
from typing import Callable

from .engine import Cancelled, TemporaryAIError
from .paths import app_data_dir

# The one model each service uses, chosen from live tests on the sample organizations (2026-11-05).
MODEL = "claude-sonnet-5-5"
EFFORT = "high"
# Models without Anthropic's server-side refusal fallback (it is rejected for them).
_NO_SERVER_FALLBACK = ("claude-haiku",)
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


def in_browser() -> bool:
    """True in the browser version (Python running inside the web page through Pyodide)."""
    return sys.platform == "emscripten"


def _browser_keys() -> dict:
    """In the browser version keys are kept in this browser's own storage for the app, on this device only."""
    try:
        keys = json.loads((app_data_dir() / "keys.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return keys if isinstance(keys, dict) else {}


def _write_browser_keys(keys: dict) -> None:
    (app_data_dir() / "keys.json").write_text(json.dumps(keys), encoding="utf-8")


def get_api_key(provider: str | None = None) -> str | None:
    p = PROVIDERS[_check_provider(provider or active_provider())]
    if in_browser():
        return _browser_keys().get(p["keyring_user"]) or None
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


def make_client(provider: str | None = None, model: str | None = None, effort: str | None = None):
    """A model client for the chosen service, or a plain-language error if no key is set up."""
    provider = _check_provider(provider or active_provider())
    key = get_api_key(provider)
    if not key:
        raise RuntimeError(f"No {PROVIDERS[provider]['label']} API key is set up yet. Add it under Settings.")
    # Tests and live evaluations may name another model or effort (also GRA_MODEL / GRA_EFFORT);
    # users never choose one.
    model = model or os.environ.get("GRA_MODEL", "").strip() or None
    effort = effort or os.environ.get("GRA_EFFORT", "").strip() or None
    if provider == "openai":
        from .llm_openai import EFFORT as OPENAI_EFFORT, MODEL as OPENAI_MODEL, OpenAIClient

        if in_browser():
            from .llm_web import BrowserOpenAI

            return OpenAIClient(key, model or OPENAI_MODEL, effort or OPENAI_EFFORT, client=BrowserOpenAI(key))
        return OpenAIClient(key, model or OPENAI_MODEL, effort or OPENAI_EFFORT)
    if in_browser():
        from .llm_web import BrowserClaudeClient

        return BrowserClaudeClient(key, model or MODEL, effort or EFFORT)
    return ClaudeClient(key, model or MODEL, effort or EFFORT)


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
    if in_browser():
        keys = _browser_keys()
        keys[PROVIDERS[_check_provider(provider)]["keyring_user"]] = key.strip()
        _write_browser_keys(keys)
        return
    import keyring

    keyring.set_password(_KEYRING_SERVICE, PROVIDERS[_check_provider(provider)]["keyring_user"], key.strip())


def delete_api_key(provider: str) -> None:
    """Forget a saved key. A key set in an environment variable is not touched."""
    user = PROVIDERS[_check_provider(provider)]["keyring_user"]
    if in_browser():
        keys = _browser_keys()
        keys.pop(user, None)
        _write_browser_keys(keys)
        return
    try:
        import keyring

        keyring.delete_password(_KEYRING_SERVICE, user)
    except Exception:
        pass


def check_api_key(key: str, provider: str = DEFAULT_PROVIDER) -> str | None:
    """Return None if the key works, else a plain-language problem."""
    if in_browser():
        from .llm_web import check_api_key as check_in_browser

        return check_in_browser(key, provider)
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


# Replies that mean the AI service had a passing problem on its side, worth waiting out and sending again.
TEMPORARY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}
_TEMPORARY_TYPES = ("overloaded", "api_error", "server_error", "rate_limit", "timeout")


def temporary_problem(code: int, detail: str) -> str | None:
    """A plain-language message if an error reply is a passing problem, else None.

    Code 200 is an error sent in the middle of a streamed reply, which is how a service that becomes
    overloaded part-way through a long answer reports it."""
    d = detail.lower()
    if "credit balance" in d or "insufficient_quota" in d or "exceeded your current quota" in d:
        return None
    if "overloaded" in d or code == 529:
        return "The AI service is overloaded right now."
    if code == 429 or "rate_limit" in d:
        return "The AI service is busy (rate limit)."
    if code in TEMPORARY_STATUS or code >= 500 or (code in (0, 200) and any(t in d for t in _TEMPORARY_TYPES)):
        return f"The AI service had a temporary problem ({code or 'connection'})."
    if code == 200:
        return "The AI service's reply was interrupted."
    return None


def is_connection_error(e: Exception) -> bool:
    """A dropped or timed-out connection, including one lost part-way through a streamed reply."""
    module = type(e).__module__.split(".")[0]  # httpx, httpx2 (newer Anthropic SDK), httpcore...
    return module.startswith(("httpx", "httpcore")) or isinstance(e, (ConnectionError, TimeoutError))


class ClaudeClient:
    provider = "anthropic"

    def __init__(self, api_key: str, model: str = MODEL, effort: str = EFFORT):
        import anthropic

        self.client = anthropic.Anthropic(api_key=api_key, max_retries=4)
        self.model = model
        self.effort = effort

    def request(self, system: str, messages: list, tools: list) -> dict:
        params = dict(model=self.model, max_tokens=64000, system=system, messages=messages, tools=tools,
                      thinking={"type": "adaptive"}, output_config={"effort": self.effort},
                      cache_control={"type": "ephemeral"})
        if not self.model.startswith(_NO_SERVER_FALLBACK):
            params.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        return params

    def respond(self, system: str, messages: list, tools: list, should_cancel: Callable[[], bool]):
        import anthropic

        try:
            with self.client.beta.messages.stream(**self.request(system, messages, tools)) as stream:
                for _ in stream:
                    if should_cancel():
                        raise Cancelled()
                return stream.get_final_message()
        except anthropic.AuthenticationError:
            raise RuntimeError("The saved API key was not accepted. Enter a new key in Settings.") from None
        except anthropic.APIConnectionError:  # also covers timeouts; the SDK has already retried
            raise TemporaryAIError("Could not reach the AI service (the connection dropped or timed out).") from None
        except anthropic.APIStatusError as e:
            temporary = temporary_problem(e.status_code, str(e))
            if temporary:
                raise TemporaryAIError(temporary) from None
            raise RuntimeError(status_message(e.status_code, str(e))) from None
        except Exception as e:
            if is_connection_error(e):
                raise TemporaryAIError("The connection to the AI service dropped part-way through a reply.") from None
            raise
