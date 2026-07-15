"""LLM proxy: with ``llm_upstream`` set, the gateway forwards /v1/* to the
upstream (client's own API key forwarded, exempt from the gateway token) and
relays the reply. This makes the gateway the agent's single egress -- LLM +
tool calls behind one destination -- so the network rule stays
"destination = gateway" and never inspects TLS/SNI (ECH-immune). Disabled by
default (no upstream -> /v1/* is a normal 404)."""

from __future__ import annotations

import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from gateway.serving.http_server import _Handler


class _Upstream(BaseHTTPRequestHandler):
    received: dict = {}

    def log_message(self, *a):  # noqa: D401 -- silence
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(n)
        _Upstream.received = {
            "path": self.path,
            "x_api_key": self.headers.get("x-api-key"),
            "body": body.decode(),
            "host": self.headers.get("Host"),
        }
        payload = json.dumps({"ok": True, "echo": len(body)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _serve(handler_cls) -> HTTPServer:
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def upstream():
    srv = _serve(_Upstream)
    yield srv
    srv.shutdown()


def _post(port, path, body, headers):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read().decode()
    conn.close()
    return resp.status, data


def test_proxy_forwards_key_body_and_relays_response(upstream):
    prev, _Handler.auth = _Handler.llm_upstream, None
    _Handler.llm_upstream = f"http://127.0.0.1:{upstream.server_address[1]}"
    gw = _serve(_Handler)
    try:
        status, data = _post(
            gw.server_address[1], "/v1/messages",
            json.dumps({"model": "x", "messages": []}),
            {"x-api-key": "sk-test-123", "Content-Type": "application/json"},
        )
        assert status == 200 and '"ok": true' in data
        # upstream saw the forwarded key + body + rewritten Host
        assert _Upstream.received["x_api_key"] == "sk-test-123"
        assert _Upstream.received["path"] == "/v1/messages"
        assert '"model": "x"' in _Upstream.received["body"]
        assert _Upstream.received["host"].startswith("127.0.0.1")
    finally:
        gw.shutdown()
        _Handler.llm_upstream = prev


def test_proxy_disabled_by_default_is_404():
    prev, _Handler.auth = _Handler.llm_upstream, None
    _Handler.llm_upstream = None
    gw = _serve(_Handler)
    try:
        status, _ = _post(gw.server_address[1], "/v1/messages", "{}",
                          {"Content-Type": "application/json"})
        assert status == 404
    finally:
        gw.shutdown()
        _Handler.llm_upstream = prev
