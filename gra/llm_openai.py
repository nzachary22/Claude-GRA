"""OpenAI as the AI service, behind the same interface as the Claude client.

The reporting loop keeps one conversation format (text, document, image,
tool_use and tool_result blocks). This module translates that conversation to
the OpenAI Responses API on every request and translates each reply back, so
the privacy gate, activity record and checks work the same for both services.

Requests are sent with storage turned off (store=false), so OpenAI does not
keep the response for later retrieval. The model's encrypted reasoning items
are carried between turns inside the conversation, as the API requires.

The OpenAI SDK loads only when a request is made, like the Anthropic SDK.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Callable

from .engine import Cancelled

MODEL = "gpt-6.1-sol"
EFFORT = "xhigh"  # OpenAI's "very high"
MAX_OUTPUT_TOKENS = 64000
# Kept on a block for replay; the activity record leaves it out (see activity.plain).
ITEM = "provider_item"


# ---------------------------------------------------------------- conversation -> OpenAI

def to_input(messages: list) -> list[dict]:
    """The app's conversation as Responses API input items."""
    items: list[dict] = []
    for m in messages:
        blocks = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": str(m["content"])}]
        if m["role"] == "assistant":
            items.extend(_assistant_items(blocks))
            continue
        parts: list[dict] = []
        for b in blocks:
            b = _as_dict(b)
            kind = b.get("type")
            if kind == "tool_result":
                if parts:  # keep the order the blocks were written in
                    items.append({"role": "user", "content": parts})
                    parts = []
                out = _result_text(b.get("content"))
                items.append({"type": "function_call_output", "call_id": b.get("tool_use_id"),
                              "output": ("ERROR: " + out) if b.get("is_error") else out})
            elif kind == "text":
                parts.append({"type": "input_text", "text": b.get("text", "")})
            elif kind == "document":
                src = b.get("source") or {}
                parts.append({"type": "input_file", "filename": b.get("title") or "document.pdf",
                              "file_data": f"data:{src.get('media_type')};base64,{src.get('data', '')}"})
            elif kind == "image":
                src = b.get("source") or {}
                parts.append({"type": "input_image", "detail": "high",
                              "image_url": f"data:{src.get('media_type')};base64,{src.get('data', '')}"})
        if parts:
            items.append({"role": "user", "content": parts})
    return items


def _assistant_items(blocks) -> list[dict]:
    out = []
    for b in blocks:
        b = _as_dict(b)
        if b.get(ITEM):
            out.append(b[ITEM])
        elif b.get("type") == "tool_use":
            out.append({"type": "function_call", "call_id": b.get("id"), "name": b.get("name"),
                        "arguments": json.dumps(b.get("input") or {})})
        elif b.get("type") == "text" and b.get("text"):
            out.append({"role": "assistant", "content": b["text"]})
    return out


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(_as_dict(c).get("text", "")) for c in content)
    return str(content or "")


def to_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "name": t["name"], "description": t.get("description", ""),
             "parameters": t["input_schema"], "strict": False} for t in tools]


# ---------------------------------------------------------------- OpenAI -> conversation

def from_response(response) -> SimpleNamespace:
    """An OpenAI response as content blocks and a stop reason the loop understands."""
    blocks, refused = [], False
    for item in _get(response, "output") or []:
        raw = _dump(item)
        kind = raw.get("type")
        if kind == "reasoning":
            blocks.append({"type": "thinking", ITEM: raw})
        elif kind == "function_call":
            try:
                args = json.loads(raw.get("arguments") or "{}")
            except ValueError:
                args = {}
            blocks.append({"type": "tool_use", "id": raw.get("call_id"), "name": raw.get("name"),
                           "input": args if isinstance(args, dict) else {}, ITEM: raw})
        elif kind == "message":
            text = []
            for part in raw.get("content") or []:
                if part.get("type") == "output_text":
                    text.append(part.get("text", ""))
                elif part.get("type") == "refusal":
                    refused = True
                    text.append(part.get("refusal", ""))
            blocks.append({"type": "text", "text": "\n".join(text), ITEM: raw})
    incomplete = _dump(_get(response, "incomplete_details") or {}).get("reason")
    if refused or incomplete == "content_filter":
        stop = "refusal"
    elif incomplete == "max_output_tokens":
        stop = "max_tokens"
    elif any(b["type"] == "tool_use" for b in blocks):
        stop = "tool_use"
    else:
        stop = "end_turn"
    return SimpleNamespace(content=blocks, stop_reason=stop, model=_get(response, "model"), usage=_usage(response))


def _usage(response) -> dict:
    """Token counts in the Anthropic shape (cached input counted separately), for cost estimates."""
    u = _dump(_get(response, "usage") or {})
    cached = (u.get("input_tokens_details") or {}).get("cached_tokens") or 0
    return {"input_tokens": (u.get("input_tokens") or 0) - cached, "cache_read_input_tokens": cached,
            "cache_creation_input_tokens": 0, "output_tokens": u.get("output_tokens") or 0}


def _get(obj, key):
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def _as_dict(b) -> dict:
    return b if isinstance(b, dict) else _dump(b)


def _dump(obj) -> dict:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump(exclude_none=True)
    return dict(getattr(obj, "__dict__", {}))


# ---------------------------------------------------------------- client

def problem_message(e) -> str:
    """A plain-language description of an OpenAI API error."""
    import openai

    detail = str(e).lower()
    if isinstance(e, openai.AuthenticationError):
        return "The saved OpenAI API key was not accepted. Enter a new key in Settings."
    if "insufficient_quota" in detail or "exceeded your current quota" in detail:
        return ("Your OpenAI account is out of API credit. Add credit at platform.openai.com under "
                "Settings > Billing, then start the report again.")
    if isinstance(e, openai.RateLimitError):
        return "The AI service is busy (rate limit). Try again in a minute."
    if isinstance(e, openai.APIConnectionError):
        return "Could not reach the AI service. Check the internet connection."
    if isinstance(e, openai.APIStatusError):
        return f"The AI service returned an error ({e.status_code})."
    return f"The AI service returned an error ({type(e).__name__})."


def check_api_key(key: str) -> str | None:
    """Return None if the key works, else a plain-language problem."""
    import openai

    try:
        openai.OpenAI(api_key=key.strip(), max_retries=1).models.retrieve(MODEL)
        return None
    except openai.AuthenticationError:
        return "That API key was not accepted."
    except openai.PermissionDeniedError:
        return "That API key does not have access to the model."
    except openai.NotFoundError:
        return f"That API key does not have access to the model {MODEL}."
    except openai.APIConnectionError:
        return "Could not reach the AI service. Check the internet connection."
    except openai.APIStatusError as e:
        return f"The AI service returned an error ({e.status_code})."


class OpenAIClient:
    provider = "openai"
    stored = False  # every request is sent with store=false

    def __init__(self, api_key: str, client=None):
        if client is None:
            import openai

            client = openai.OpenAI(api_key=api_key, max_retries=4)
        self.client = client
        self.model = MODEL
        self.effort = EFFORT

    def respond(self, system: str, messages: list, tools: list, should_cancel: Callable[[], bool]):
        try:
            with self.client.responses.stream(
                model=self.model,
                instructions=system,
                input=to_input(messages),
                tools=to_tools(tools),
                reasoning={"effort": self.effort},
                max_output_tokens=MAX_OUTPUT_TOKENS,
                store=False,
                include=["reasoning.encrypted_content"],
            ) as stream:
                for _ in stream:
                    if should_cancel():
                        raise Cancelled()
                response = stream.get_final_response()
        except Cancelled:
            raise
        except Exception as e:
            if type(e).__module__.split(".")[0] != "openai":
                raise
            raise RuntimeError(problem_message(e)) from None
        if _get(response, "status") == "failed":
            raise RuntimeError("The AI service could not complete the request. Try again.")
        return from_response(response)
