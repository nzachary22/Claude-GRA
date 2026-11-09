"""A stand-in for the Anthropic and OpenAI APIs, for testing the browser version end to end.

It answers like the real services (streamed replies, the same JSON shapes, CORS headers for a
web page), with the recorded Juniper Ridge analyst from gra/demo.py playing the AI. The browser
tests point the page's AI requests here, so a full report is prepared in the browser through the
real request code, security policy and reply handling, with no key and no cost.

    python -m tests.fake_ai 8830
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from gra.demo import DemoModel

SAMPLE = Path(__file__).resolve().parents[1] / "gra" / "sample"
PERIOD = ("2026-01-01", "2026-12-31")
GOOD_KEY = "fake-key-for-browser-tests-0123456789"


class Conversations:
    """One recorded analyst per service; a request with a single message starts a new report."""

    def __init__(self):
        self.models: dict[str, DemoModel] = {}
        self.requests: list[dict] = []

    def reply(self, service: str, messages: list):
        if len(messages) == 1 or service not in self.models:
            self.models[service] = DemoModel(*PERIOD, SAMPLE, 0)
        return self.models[service].respond("", messages, [], lambda: False)


STATE = Conversations()


def sse(events: list[tuple[str, dict]]) -> bytes:
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()


def anthropic_stream(reply) -> bytes:
    events = [("message_start", {"type": "message_start", "message": {
        "id": "msg_fake", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5", "content": [],
        "stop_reason": None, "usage": {"input_tokens": 10, "output_tokens": 1}}}),
        ("ping", {"type": "ping"})]
    for i, b in enumerate(reply.content):
        if b["type"] == "tool_use":
            events.append(("content_block_start", {"type": "content_block_start", "index": i, "content_block": {
                "type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}}))
            raw = json.dumps(b["input"])
            for k in range(0, len(raw), 500):  # the input arrives in pieces, as from the real service
                events.append(("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {
                    "type": "input_json_delta", "partial_json": raw[k:k + 500]}}))
        else:
            events.append(("content_block_start", {"type": "content_block_start", "index": i,
                                                   "content_block": {"type": "text", "text": ""}}))
            events.append(("content_block_delta", {"type": "content_block_delta", "index": i,
                                                   "delta": {"type": "text_delta", "text": b.get("text", "")}}))
        events.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
    events.append(("message_delta", {"type": "message_delta", "delta": {"stop_reason": reply.stop_reason},
                                     "usage": {"output_tokens": 50}}))
    events.append(("message_stop", {"type": "message_stop"}))
    return sse(events)


def openai_messages(body: dict) -> list:
    """Enough of the conversation to know where the report is: one message per model turn."""
    turns = [{"role": "user", "content": "start"}]
    for item in body.get("input") or []:
        if item.get("type") == "function_call_output":
            if turns[-1]["role"] != "user":
                turns.append({"role": "user", "content": []})
        elif item.get("type") in ("function_call", "message") and item.get("role", "assistant") == "assistant":
            if turns[-1]["role"] != "assistant":
                turns.append({"role": "assistant", "content": []})
    return turns


def openai_stream(reply) -> bytes:
    output = []
    for b in reply.content:
        if b["type"] == "tool_use":
            output.append({"type": "function_call", "id": "fc_" + b["id"], "call_id": b["id"], "name": b["name"],
                           "arguments": json.dumps(b["input"]), "status": "completed"})
        else:
            output.append({"type": "message", "id": "msg_x", "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": b.get("text", ""), "annotations": []}]})
    response = {"id": "resp_fake", "object": "response", "status": "completed", "model": "gpt-6.1-sol",
                "output": output, "usage": {"input_tokens": 10, "output_tokens": 50}}
    return sse([("response.created", {"type": "response.created", "response": {**response, "status": "in_progress",
                                                                               "output": []}}),
                ("response.completed", {"type": "response.completed", "response": response})])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def send(self, status: int, body: bytes, kind="application/json"):
        self.send_response(status)
        self.cors()
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send(200, b"")

    def authorized(self) -> bool:
        key = self.headers.get("x-api-key") or (self.headers.get("authorization") or "").removeprefix("Bearer ")
        if key != GOOD_KEY:
            self.send(401, json.dumps({"type": "error", "error": {"type": "authentication_error",
                                                                  "message": "invalid x-api-key"}}).encode())
            return False
        return True

    def do_GET(self):
        if not self.authorized():
            return
        if self.path.startswith("/v1/models/"):
            self.send(200, json.dumps({"id": self.path.rsplit("/", 1)[1], "type": "model"}).encode())
        else:
            self.send(404, b"{}")

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        STATE.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
        if not self.authorized():
            return
        if self.path == "/v1/messages":
            reply = STATE.reply("anthropic", body.get("messages") or [])
            self.send(200, anthropic_stream(reply), "text/event-stream")
        elif self.path == "/v1/responses":
            reply = STATE.reply("openai", openai_messages(body))
            self.send(200, openai_stream(reply), "text/event-stream")
        else:
            self.send(404, b"{}")


def serve(port: int):
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8830)
