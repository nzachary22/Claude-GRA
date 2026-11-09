"""AI connections for the browser version: the page talks to Anthropic or OpenAI directly.

In the browser the Python code runs inside a background worker of the web page
(Pyodide), where the AI companies' Python libraries cannot run. These small
clients send the same requests over the browser's own connection instead, and
return replies in the shape the report loop already understands. Requests ask
for a streamed reply, which keeps a long-thinking request alive on slow
networks; the worker waits for the whole reply and puts it back together.

Nothing else is contacted: the page's security policy only allows these two
addresses (web/_headers).
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

ANTHROPIC_URL = "https://api.anthropic.com/v1"
OPENAI_URL = "https://api.openai.com/v1"
RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}
RETRIES = 4


def _xhr(method: str, url: str, headers: dict, body: str | None) -> tuple[int, str]:
    """One synchronous request through the browser (allowed in a worker). Status 0 = no connection."""
    from js import XMLHttpRequest  # only inside Pyodide

    x = XMLHttpRequest.new()
    x.open(method, url, False)
    for k, v in headers.items():
        x.setRequestHeader(k, v)
    try:
        x.send(body)
    except Exception:
        return 0, ""
    return int(x.status), str(x.responseText or "")


transport = _xhr  # tests replace this


def _request(method: str, url: str, headers: dict, body: dict | None = None, retries: int = RETRIES) -> tuple[int, str]:
    data = json.dumps(body) if body is not None else None
    for attempt in range(retries + 1):
        status, text = transport(method, url, headers, data)
        if status not in RETRY_STATUS and status != 0:
            return status, text
        if attempt < retries:
            time.sleep(min(2 ** attempt, 16))
    return status, text


def parse_sse(text: str):
    """(event, data) pairs from a server-sent event stream."""
    for chunk in text.replace("\r\n", "\n").split("\n\n"):
        event, data = None, []
        for line in chunk.split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
        if data:
            try:
                yield event, json.loads("\n".join(data))
            except ValueError:
                continue


def _error_detail(text: str) -> str:
    try:
        err = json.loads(text).get("error") or {}
        return f"{err.get('type', '')} {err.get('message', '')} {err.get('code', '')}"
    except (ValueError, AttributeError):
        return text[:500]


# ---------------------------------------------------------------- Anthropic

def _anthropic_headers(key: str, betas: list[str] | None = None) -> dict:
    h = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json",
         "anthropic-dangerous-direct-browser-access": "true"}
    if betas:
        h["anthropic-beta"] = ",".join(betas)
    return h


def assemble_message(text: str) -> dict:
    """The final message from a streamed Messages API reply."""
    message, blocks, partial = {}, {}, {}
    for event, data in parse_sse(text):
        kind = data.get("type") or event
        if kind == "error":
            raise RuntimeError(_anthropic_problem(500, json.dumps(data)))
        if kind == "message_start":
            message = dict(data.get("message") or {})
        elif kind == "content_block_start":
            blocks[data["index"]] = dict(data.get("content_block") or {})
        elif kind == "content_block_delta":
            b, d = blocks.setdefault(data["index"], {}), data.get("delta") or {}
            dt = d.get("type")
            if dt == "text_delta":
                b["text"] = b.get("text", "") + d.get("text", "")
            elif dt == "thinking_delta":
                b["thinking"] = b.get("thinking", "") + d.get("thinking", "")
            elif dt == "signature_delta":
                b["signature"] = b.get("signature", "") + d.get("signature", "")
            elif dt == "input_json_delta":
                partial[data["index"]] = partial.get(data["index"], "") + d.get("partial_json", "")
            elif dt == "citations_delta":
                b.setdefault("citations", []).append(d.get("citation"))
        elif kind == "message_delta":
            message.update({k: v for k, v in (data.get("delta") or {}).items() if v is not None})
            if data.get("usage"):
                message["usage"] = {**(message.get("usage") or {}), **data["usage"]}
    for i, raw in partial.items():
        try:
            blocks[i]["input"] = json.loads(raw) if raw.strip() else {}
        except ValueError:
            blocks[i]["input"] = {}
    message["content"] = [blocks[i] for i in sorted(blocks)]
    if "stop_reason" not in message:
        raise RuntimeError("The AI service's reply was cut off. Try again.")
    return message


def _anthropic_problem(status: int, text: str) -> str:
    from .llm import status_message

    if status == 0:
        return "Could not reach the AI service. Check the internet connection."
    if status == 401:
        return "The saved API key was not accepted. Enter a new key in Settings."
    if status == 429:
        return "The AI service is busy (rate limit). Try again in a minute."
    return status_message(status, _error_detail(text))


class BrowserClaudeClient:
    """The same requests as llm.ClaudeClient, sent from the browser."""
    provider = "anthropic"

    def __init__(self, api_key: str, model: str, effort: str):
        from .llm import ClaudeClient

        self.key = api_key
        self.model = model
        self.effort = effort
        self._params = ClaudeClient.request.__get__(self)  # the desktop client's exact request

    def respond(self, system: str, messages: list, tools: list, should_cancel):
        params = self._params(system, messages, tools)
        betas = params.pop("betas", None)
        params["stream"] = True
        status, text = _request("POST", f"{ANTHROPIC_URL}/messages", _anthropic_headers(self.key, betas), params)
        if status != 200:
            raise RuntimeError(_anthropic_problem(status, text))
        message = assemble_message(text)
        return SimpleNamespace(content=message["content"], stop_reason=message.get("stop_reason"),
                               model=message.get("model"), usage=message.get("usage"))


# ---------------------------------------------------------------- OpenAI

def _openai_headers(key: str) -> dict:
    return {"authorization": f"Bearer {key}", "content-type": "application/json"}


def _openai_problem(status: int, text: str) -> str:
    detail = _error_detail(text).lower()
    if status == 0:
        return "Could not reach the AI service. Check the internet connection."
    if status == 401:
        return "The saved OpenAI API key was not accepted. Enter a new key in Settings."
    if "insufficient_quota" in detail or "exceeded your current quota" in detail:
        return ("Your OpenAI account is out of API credit. Add credit at platform.openai.com under "
                "Settings > Billing, then start the report again.")
    if status == 429:
        return "The AI service is busy (rate limit). Try again in a minute."
    return f"The AI service returned an error ({status})."


class _Stream:
    def __init__(self, response: dict):
        self.response = response

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(())

    def get_final_response(self):
        return self.response


class BrowserOpenAI:
    """Just enough of the OpenAI client for llm_openai.OpenAIClient: responses.stream(...)."""

    def __init__(self, api_key: str):
        self.key = api_key
        self.responses = self

    def stream(self, **params):
        params["stream"] = True
        status, text = _request("POST", f"{OPENAI_URL}/responses", _openai_headers(self.key), params)
        if status != 200:
            raise RuntimeError(_openai_problem(status, text))
        final = None
        for _, data in parse_sse(text):
            kind = data.get("type", "")
            if kind in ("response.completed", "response.incomplete", "response.failed"):
                final = data.get("response")
            elif kind == "error":
                raise RuntimeError(_openai_problem(500, json.dumps({"error": data})))
        if final is None:
            raise RuntimeError("The AI service's reply was cut off. Try again.")
        return _Stream(final)


# ---------------------------------------------------------------- key check

def check_api_key(key: str, provider: str) -> str | None:
    """Return None if the key works, else a plain-language problem."""
    from .llm import MODEL
    from .llm_openai import MODEL as OPENAI_MODEL

    key = key.strip()
    if provider == "openai":
        status, text = _request("GET", f"{OPENAI_URL}/models/{OPENAI_MODEL}", _openai_headers(key), retries=1)
        model = OPENAI_MODEL
    else:
        status, text = _request("GET", f"{ANTHROPIC_URL}/models/{MODEL}", _anthropic_headers(key), retries=1)
        model = MODEL
    if status == 200:
        return None
    if status == 0:
        return "Could not reach the AI service. Check the internet connection."
    if status == 401:
        return "That API key was not accepted."
    if status in (403, 404):
        return f"That API key does not have access to the model {model}."
    return f"The AI service returned an error ({status})."
