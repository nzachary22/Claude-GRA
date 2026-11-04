"""Claude client and API key storage.

The key is entered once in the app and kept in the operating system's
credential store (Windows Credential Manager), never in the repository.

The Anthropic SDK can take close to a minute to import on a slow or busy
computer, so it loads only when needed and the page never waits on it.
"""

from __future__ import annotations

import os
from typing import Callable

from .engine import Cancelled

MODEL = "claude-opus-5-5"
_KEYRING_SERVICE = "GrantReportingAssistant"
_KEYRING_USER = "anthropic_api_key"


def get_api_key() -> str | None:
    env = os.environ.get("ANTHROPIC_API_KEY") or _windows_user_env("ANTHROPIC_API_KEY")
    if env:
        return env
    try:
        import keyring

        return keyring.get_password(_KEYRING_SERVICE, _KEYRING_USER)
    except Exception:
        return None


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


def save_api_key(key: str) -> None:
    import keyring

    keyring.set_password(_KEYRING_SERVICE, _KEYRING_USER, key.strip())


def check_api_key(key: str) -> str | None:
    """Return None if the key works, else a plain-language problem."""
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


class ClaudeClient:
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
            raise RuntimeError(f"The AI service returned an error ({e.status_code}).") from None
