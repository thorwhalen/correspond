"""The standard-library transport: credentials never follow a redirect to another host."""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from correspond.channels._http import urllib_http


def _serve(answer):
    """A local server on a free port; ``answer(handler)`` replies; returns (server, seen headers)."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append({k.lower(): v for k, v in self.headers.items()})
            answer(self)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


def _ok(handler):
    handler.send_response(200)
    handler.send_header("Content-Length", "2")
    handler.end_headers()
    handler.wfile.write(b"ok")


@pytest.fixture
def target():
    server, seen = _serve(_ok)
    yield f"http://127.0.0.1:{server.server_port}", seen
    server.shutdown()
    server.server_close()


def _redirecting_to(location):
    def answer(handler):
        if handler.path == "/here":
            return _ok(handler)
        handler.send_response(302)
        handler.send_header("Location", location)
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    return answer


def test_a_redirect_to_another_host_goes_on_without_the_token(target):
    elsewhere, seen_there = target
    server, seen = _serve(_redirecting_to(f"{elsewhere}/landing"))
    try:
        url = f"http://127.0.0.1:{server.server_port}/start"
        reply = urllib_http("GET", url, headers={"Authorization": "Bearer fake", "X-Kept": "1"})
    finally:
        server.shutdown()
        server.server_close()
    assert reply.status == 200
    assert seen[0]["authorization"] == "Bearer fake"
    assert "authorization" not in seen_there[0] and seen_there[0]["x-kept"] == "1"


def test_a_redirect_on_the_same_host_keeps_the_token():
    server, seen = _serve(_redirecting_to("/here"))
    try:
        url = f"http://127.0.0.1:{server.server_port}/start"
        reply = urllib_http("GET", url, headers={"Authorization": "Bearer fake"})
    finally:
        server.shutdown()
        server.server_close()
    assert reply.status == 200
    assert [h.get("authorization") for h in seen] == ["Bearer fake", "Bearer fake"]
