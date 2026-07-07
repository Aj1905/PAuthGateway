"""Observability: file-backed audit trail + value-free health/status.

Engineering hardening for DESIGN_STATUS "Observable health" and bottleneck #4
(audit log). The status surface is deliberately value-free so it is safe on the
unauthenticated localhost health endpoint (unlike the audit log, which is
operator-facing and may quote operand values).
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer

import pytest

from pauth.suites.shopping import build_suite as build_shopping_suite

from gateway.ingress.agent_channel import AgentChannel
from gateway.runtime.audit import AuditLog
from gateway.runtime.gateway import Gateway
from gateway.serving import http_server

# A prompt the deterministic recognizer accepts for the shopping suite.
ACCEPTED_PROMPT = (
    'If the product "Aurora Noise Cancelling Headphones" is in stock and costs '
    'less than $150.00, add 1 to my cart and pay the cart total to IBAN '
    'GB33BUKB20201555555555 with subject "Order payment" on 2024-06-11.'
)
SECRET_MARKERS = ("GB33BUKB20201555555555", "Aurora")


def _loader(name):
    if name != "shopping":
        raise ValueError(name)
    return build_shopping_suite()


# ---------------------------------------------------------------------------
# Audit-log persistence
# ---------------------------------------------------------------------------

def test_audit_log_persists_jsonl(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.record("submit", "accept", reason_code="accepted", reason="ok")
    log.record("tool_call", "deny", tool="bash", reason_code="side_channel_denied", reason="no")

    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    e0, e1 = json.loads(lines[0]), json.loads(lines[1])
    assert (e0["seq"], e0["kind"], e0["decision"]) == (0, "submit", "accept")
    assert (e1["seq"], e1["tool"], e1["decision"]) == (1, "bash", "deny")


def test_injected_audit_log_is_used_and_persisted(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    gw = Gateway(_loader, audit_log=log)
    gw.submit_user_prompt(ACCEPTED_PROMPT)

    # the gateway recorded through the injected (persistent) log
    assert len(gw.audit_log()) == len(log) >= 1
    assert len(path.read_text().strip().splitlines()) == len(log)


def test_audit_log_in_memory_by_default(tmp_path):
    # No path -> nothing written, behaviour unchanged.
    log = AuditLog()
    log.record("submit", "accept")
    assert len(log) == 1
    assert not (tmp_path / "audit.jsonl").exists()


# ---------------------------------------------------------------------------
# Value-free status
# ---------------------------------------------------------------------------

def test_gateway_status_after_accept_is_value_free():
    gw = Gateway(_loader)
    gw.submit_user_prompt(ACCEPTED_PROMPT)
    s = gw.status()

    assert s["plan_active"] is True
    assert s["rule_count"] >= 1
    assert s["pending_confirmations"] == 0
    assert s["protection"]["level"] in {"L2", "L3"}
    blob = json.dumps(s)
    for marker in SECRET_MARKERS:
        assert marker not in blob  # no operand value leaks into status


def test_gateway_status_after_reject_reports_value_free_reason_code():
    gw = Gateway(_loader)
    gw.submit_user_prompt("please do something completely unrecognized zzz")
    s = gw.status()

    assert s["plan_active"] is False
    assert s["rule_count"] == 0
    assert s["reason_code"] is None or isinstance(s["reason_code"], str)


def test_agentchannel_status_tracks_prompt_and_plan():
    ch = AgentChannel(_loader)
    assert ch.status()["prompt_received"] is False

    ch.receive_json({"kind": "prompt", "prompt": ACCEPTED_PROMPT, "strategy": "deterministic"})
    st = ch.status()
    assert st["prompt_received"] is True
    assert st["plan_active"] is True


# ---------------------------------------------------------------------------
# HTTP health / status endpoints
# ---------------------------------------------------------------------------

def _start_server():
    http_server._Handler.suite_loader = staticmethod(_loader)
    http_server._Handler.sessions = {}
    http_server._Handler.session_store = None
    http_server._Handler.audit_log = None
    srv = HTTPServer(("127.0.0.1", 0), http_server._Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv


def _get(url):
    with urllib.request.urlopen(url) as r:
        return r.status, json.loads(r.read())


def _post(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req) as r:
        return r.status, json.loads(r.read())


def test_http_health_and_session_status():
    srv = _start_server()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"

        status, body = _get(base + "/health")
        assert status == 200 and body["status"] == "ok"
        assert body["sessions"] == 0

        sid = "obs-test-session"
        _post(f"{base}/sessions/{sid}/messages",
              {"kind": "prompt", "prompt": ACCEPTED_PROMPT, "strategy": "deterministic"})

        status, body = _get(f"{base}/sessions/{sid}")
        assert status == 200
        assert body["session_id"] == sid
        assert body["prompt_received"] is True
        assert body["plan_active"] is True
        for marker in SECRET_MARKERS:
            assert marker not in json.dumps(body)  # value-free wire surface

        # unknown session -> 404
        code = None
        try:
            _get(f"{base}/sessions/nope")
        except urllib.error.HTTPError as exc:
            code = exc.code
        assert code == 404
    finally:
        srv.shutdown()


def test_http_health_reports_audit_persistence_flag(tmp_path):
    srv = _start_server()
    http_server._Handler.audit_log = AuditLog(tmp_path / "audit.jsonl")
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        _, body = _get(base + "/health")
        assert body["audit_persisted"] is True
    finally:
        http_server._Handler.audit_log = None
        srv.shutdown()
