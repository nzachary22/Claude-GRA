"""A small Model Context Protocol (MCP) server over standard input and output.

The Claude app starts the connector as a local program and talks to it with
JSON-RPC messages, one per line. Only the parts of MCP the connector uses are
here: the handshake, listing tools and calling them. It uses the standard
library only, and older Python syntax, so it can still start (and explain the
problem) when the computer's Python is too old for the rest of the app.
"""

import json
import sys
import threading
import traceback

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")


class ToolServer(object):
    """name, version and instructions describe the server; tools() lists tool definitions
    (MCP form: name, description, inputSchema); call(name, args) returns (text, is_error)."""

    def __init__(self, name, version, instructions, tools, call):
        self.name = name
        self.version = version
        self.instructions = instructions
        self.tools = tools
        self.call = call
        self._out_lock = threading.Lock()

    def serve(self, stdin=None, stdout=None):
        stdin = stdin or sys.stdin.buffer
        stdout = stdout or sys.stdout.buffer
        # Anything printed by mistake must never mix with the protocol stream.
        sys.stdout = sys.stderr
        for raw in iter(stdin.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self._send(stdout, {"jsonrpc": "2.0", "id": None,
                                    "error": {"code": -32700, "message": "Parse error"}})
                continue
            for reply in self._handle_any(message):
                self._send(stdout, reply)

    def _handle_any(self, message):
        batch = message if isinstance(message, list) else [message]
        replies = []
        for m in batch:
            reply = self.handle(m)
            if reply is not None:
                replies.append(reply)
        return replies

    def handle(self, m):
        """One JSON-RPC message in, its reply out (None for notifications)."""
        if not isinstance(m, dict):
            return None
        method, mid, params = m.get("method"), m.get("id"), m.get("params") or {}
        if mid is None:  # a notification, such as notifications/initialized or cancelled
            return None
        try:
            if method == "initialize":
                asked = params.get("protocolVersion")
                version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
                result = {"protocolVersion": version,
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": self.name, "version": self.version},
                          "instructions": self.instructions}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self.tools()}
            elif method == "tools/call":
                name = params.get("name")
                args = params.get("arguments") or {}
                try:
                    text, is_error = self.call(name, args)
                except Exception as e:  # a tool failure is reported to the AI, not a protocol error
                    traceback.print_exc(file=sys.stderr)
                    text, is_error = "The connector hit an unexpected problem: %s: %s" % (type(e).__name__, e), True
                result = {"content": [{"type": "text", "text": text}], "isError": bool(is_error)}
            elif method in ("resources/list", "prompts/list"):
                result = {"resources": []} if method == "resources/list" else {"prompts": []}
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found: %s" % method}}
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def _send(self, stdout, obj):
        data = (json.dumps(obj, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        with self._out_lock:
            stdout.write(data)
            stdout.flush()
