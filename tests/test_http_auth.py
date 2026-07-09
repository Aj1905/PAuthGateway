"""HTTP auth + session-ownership tests (the auth layer over AgentChannel)."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer

from pauth.suites.shopping import build_suite as build_shopping_suite

from gateway.serving import http_server
from gateway.serving.http_server import TokenAuth


def _loader(name):
    if name != "shopping":
        raise ValueError(name)
    return build_shopping_suite()


def _serve(auth=None):
    http_server._Handler.suite_loader = staticmethod(_loader)
    http_server._Handler.sessions = {}
    http_server._Handler.session_owners = {}
    http_server._Handler.session_store = None
    http_server._Handler.audit_log = None
    http_server._Handler.auth = auth
    srv = HTTPServer(("127.0.0.1", 0), http_server._Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _req(method, url, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, None


# --- TokenAuth unit --------------------------------------------------------

def test_token_auth_maps_token_to_principal():
    auth = TokenAuth({"alice": "tok-a", "bob": "tok-b"})
    assert auth.principal_for("Bearer tok-a") == "alice"
    assert auth.principal_for("Bearer tok-b") == "bob"
    assert auth.principal_for("Bearer wrong") is None
    assert auth.principal_for(None) is None
    assert auth.principal_for("tok-a") is None  # missing "Bearer "


def test_token_auth_rejects_empty_tokens():
    auth = TokenAuth({"x": ""})
    assert auth.principal_for("Bearer ") is None


# --- open mode (backward compatible) --------------------------------------

def test_open_mode_allows_unauthenticated():
    srv = _serve(auth=None)
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        st, body = _req("POST", base + "/sessions", body={})
        assert st == 201 and "session_id" in body
    finally:
        srv.shutdown()


# --- auth enforced ---------------------------------------------------------

def test_auth_required_and_enforced():
    srv = _serve(auth=TokenAuth({"operator": "secret"}))
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        # no token -> 401
        assert _req("POST", base + "/sessions", body={})[0] == 401
        # wrong token -> 401
        assert _req("POST", base + "/sessions", token="nope", body={})[0] == 401
        # valid token -> 201
        st, body = _req("POST", base + "/sessions", token="secret", body={})
        assert st == 201
        # /health stays open (liveness)
        assert _req("GET", base + "/health")[0] == 200
    finally:
        srv.shutdown()


# --- session ownership (IDOR) ---------------------------------------------

def test_session_is_bound_to_creating_principal():
    srv = _serve(auth=TokenAuth({"alice": "tok-a", "bob": "tok-b"}))
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        sid = "shared-id"
        # alice creates a session under a client-supplied id via a prompt
        st, _ = _req("POST", f"{base}/sessions/{sid}/messages", token="tok-a",
                     body={"kind": "prompt", "prompt": "x", "strategy": "deterministic"})
        assert st == 200
        # bob cannot read it (404, existence hidden)
        assert _req("GET", f"{base}/sessions/{sid}", token="tok-b")[0] == 404
        # bob cannot inject into it (403)
        assert _req("POST", f"{base}/sessions/{sid}/messages", token="tok-b",
                    body={"kind": "tool_call", "tool": "x", "args": []})[0] == 403
        # bob cannot delete it (404)
        assert _req("DELETE", f"{base}/sessions/{sid}", token="tok-b")[0] == 404
        # alice still can read it
        assert _req("GET", f"{base}/sessions/{sid}", token="tok-a")[0] == 200
    finally:
        srv.shutdown()
