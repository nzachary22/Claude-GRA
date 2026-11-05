"""Starting the app a second time opens the running one instead of failing."""

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from gra.server import _port_state


def _serve(body: bytes):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert _port_state(port) == "free"


def test_port_used_by_this_app():
    srv = _serve(b"<html><head><title>Grant Reporting Assistant</title></head></html>")
    try:
        assert _port_state(srv.server_port) == "gra"
    finally:
        srv.shutdown()


def test_port_used_by_another_program():
    srv = _serve(b"<html><title>Something else</title></html>")
    try:
        assert _port_state(srv.server_port) == "other"
    finally:
        srv.shutdown()
