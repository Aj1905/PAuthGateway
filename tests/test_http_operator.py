"""Operator surface over HTTP: holds are visible to and decidable by the human only.

The agent token must never list or resolve a hold (an injection-victim agent
would otherwise approve its own off-plan call). The human presents a separate
operator token.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer

from pauth.suites.shopping import build_suite as build_shopping_suite

from gateway.operator.cli import OperatorClient, render_hold
from gateway.serving import http_server
from gateway.serving.http_server import TokenAuth

AURORA = (
    'If the product "Aurora Noise Cancelling Headphones" is in stock and costs '
    "less than $150, add 1 to my cart and pay the cart total to IBAN "
    'GB33BUKB20201555555555 with subject "Order payment" on 2026-08-22.'
)


def _loader(name):
    if name != "shopping":
        raise ValueError(name)
    return build_shopping_suite()


def _serve(auth=None, operator_token=None):
    http_server._Handler.suite_loader = staticmethod(_loader)
    http_server._Handler.sessions = {}
    http_server._Handler.session_owners = {}
    http_server._Handler.session_store = None
    http_server._Handler.audit_log = None
    http_server._Handler.auth = auth
    http_server._Handler.operator_token = operator_token
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
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, (json.loads(raw) if raw else None)


def _plan_and_hold(base, agent_token):
    """Submit the Aurora plan, then request an off-plan call so a hold exists."""
    sid = "s1"
    st, body = _req("POST", f"{base}/sessions/{sid}/messages", token=agent_token,
                    body={"kind": "prompt", "prompt": AURORA, "strategy": "deterministic"})
    assert st == 200 and body["accepted"], body
    st, body = _req("POST", f"{base}/sessions/{sid}/messages", token=agent_token,
                    body={"kind": "tool_call", "tool": "list_products", "kwargs": {"category": "audio", "max_price": 200.0}})
    assert st == 200 and body["permit"] is False, body
    assert body["reauthorization_required"] is True
    return sid


def test_agent_token_cannot_reach_operator_surface():
    srv = _serve(auth=TokenAuth({"agent": "tok-agent"}), operator_token="tok-human")
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        sid = _plan_and_hold(base, "tok-agent")
        assert _req("GET", f"{base}/sessions", token="tok-agent")[0] == 403
        assert _req("GET", f"{base}/sessions/{sid}/pending", token="tok-agent")[0] == 403
        assert _req("POST", f"{base}/sessions/{sid}/decisions", token="tok-agent",
                    body={"kind": "reauthorization", "id": "r0", "approved": True})[0] == 403
        # No token / wrong token -> 401
        assert _req("GET", f"{base}/sessions")[0] == 401
        assert _req("GET", f"{base}/sessions", token="nope")[0] == 401
    finally:
        srv.shutdown()


def test_operator_surface_disabled_without_token():
    srv = _serve(auth=TokenAuth({"agent": "tok-agent"}), operator_token=None)
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        sid = _plan_and_hold(base, "tok-agent")
        assert _req("GET", f"{base}/sessions/{sid}/pending", token="tok-agent")[0] == 404
    finally:
        srv.shutdown()


def test_operator_sees_hold_with_values_and_approval_permits_one_retry():
    srv = _serve(auth=TokenAuth({"agent": "tok-agent"}), operator_token="tok-human")
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        sid = _plan_and_hold(base, "tok-agent")

        client = OperatorClient(base, "tok-human")
        sessions = client.sessions()
        assert [s["session_id"] for s in sessions] == [sid]
        assert sessions[0]["pending_reauthorizations"] == 1

        pending = client.pending(sid)
        assert pending["confirmations"] == []
        [hold] = pending["reauthorizations"]
        assert hold["kind"] == "reauthorization" and hold["tool"] == "list_products"
        assert "list_products" in render_hold(sid, hold)

        # The agent's status stays value-free but counts the hold.
        st, status = _req("GET", f"{base}/sessions/{sid}", token="tok-agent")
        assert st == 200 and status["pending_reauthorizations"] == 1

        assert client.decide(sid, "reauthorization", hold["id"], approved=True) is True
        # Stale id -> not resolved (409)
        assert client.decide(sid, "reauthorization", hold["id"], approved=True) is False

        st, body = _req("POST", f"{base}/sessions/{sid}/messages", token="tok-agent",
                        body={"kind": "tool_call", "tool": "list_products", "kwargs": {"category": "audio", "max_price": 200.0}})
        assert st == 200 and body["permit"] is True, body
        assert body["execution_status"] == "succeeded"
        # Single use: a second identical call is held again, not permitted.
        st, body = _req("POST", f"{base}/sessions/{sid}/messages", token="tok-agent",
                        body={"kind": "tool_call", "tool": "list_products", "kwargs": {"category": "audio", "max_price": 200.0}})
        assert st == 200 and body["permit"] is False
    finally:
        srv.shutdown()


def test_operator_rejection_is_remembered():
    srv = _serve(auth=TokenAuth({"agent": "tok-agent"}), operator_token="tok-human")
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        sid = _plan_and_hold(base, "tok-agent")
        client = OperatorClient(base, "tok-human")
        [hold] = client.pending(sid)["reauthorizations"]
        assert client.decide(sid, "reauthorization", hold["id"], approved=False) is True
        st, body = _req("POST", f"{base}/sessions/{sid}/messages", token="tok-agent",
                        body={"kind": "tool_call", "tool": "list_products", "kwargs": {"category": "audio", "max_price": 200.0}})
        assert body["permit"] is False
        # Rejected action does not re-enter the queue.
        assert client.pending(sid)["reauthorizations"] == []
    finally:
        srv.shutdown()


def test_decision_validation():
    srv = _serve(auth=None, operator_token="tok-human")
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        sid = _plan_and_hold(base, None)
        assert _req("POST", f"{base}/sessions/{sid}/decisions", token="tok-human",
                    body={"kind": "bogus", "id": "r0", "approved": True})[0] == 400
        assert _req("POST", f"{base}/sessions/{sid}/decisions", token="tok-human",
                    body={"kind": "reauthorization", "id": "r0", "approved": "yes"})[0] == 400
        assert _req("POST", f"{base}/sessions/nope/decisions", token="tok-human",
                    body={"kind": "reauthorization", "id": "r0", "approved": True})[0] == 404
    finally:
        srv.shutdown()
