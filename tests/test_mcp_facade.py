"""The MCP facade: the gateway as the agent's only MCP server.

Runs the real HTTP daemon in-thread (shopping suite) and the facade as a
subprocess speaking newline-delimited JSON-RPC, exactly as an MCP client
(Claude Code) would.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

from pauth.suites.shopping import build_suite as build_shopping_suite

from gateway.hooks.local_policy import decide
from gateway.serving import http_server
from gateway.serving.http_server import TokenAuth
from gateway.serving.mcp_facade import Daemon, Facade, resolve_session

ROOT = Path(__file__).resolve().parents[1]
AURORA = (
    'If the product "Aurora Noise Cancelling Headphones" is in stock and costs '
    "less than $150, add 1 to my cart and pay the cart total to IBAN "
    'GB33BUKB20201555555555 with subject "Order payment" on 2026-08-22.'
)


def _loader(name):
    if name != "shopping":
        raise ValueError(name)
    return build_shopping_suite()


def _serve(auth=None):
    http_server._Handler.suite_loader = staticmethod(_loader)
    http_server._Handler.sessions = {}
    http_server._Handler.session_owners = {}
    http_server._Handler.bindings = {}
    http_server._Handler.merged_suite_name = "shopping"
    http_server._Handler.session_store = None
    http_server._Handler.audit_log = None
    http_server._Handler.auth = auth
    http_server._Handler.operator_token = None
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


def test_tools_route_describes_surface_with_schemas():
    srv = _serve(auth=TokenAuth({"agent": "tok"}))
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        assert _req("GET", base + "/tools")[0] == 401
        st, body = _req("GET", base + "/tools", token="tok")
        assert st == 200 and body["suite"] == "shopping"
        by_name = {t["name"]: t for t in body["tools"]}
        assert set(by_name) >= {"get_product_details", "send_money"}
        schema = by_name["send_money"]["inputSchema"]
        assert schema["type"] == "object"
        assert list(schema["properties"]) == ["recipient", "amount", "subject", "date"]
    finally:
        srv.shutdown()


def test_binding_maps_agent_process_to_its_latest_session():
    srv = _serve(auth=TokenAuth({"agent": "tok", "other": "tok2"}))
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        assert _req("GET", base + "/bindings/pid:42", token="tok")[0] == 404
        for sid in ("s-old", "s-new"):
            st, body = _req("POST", f"{base}/sessions/{sid}/messages", token="tok",
                            body={"kind": "prompt", "prompt": AURORA,
                                  "strategy": "deterministic", "binding": "pid:42"})
            assert st == 200 and body["accepted"], body
        st, body = _req("GET", base + "/bindings/pid:42", token="tok")
        assert st == 200 and body["session_id"] == "s-new"
        # Another principal cannot resolve someone else's binding.
        assert _req("GET", base + "/bindings/pid:42", token="tok2")[0] == 404
        # A rejected prompt does not (re)bind.
        st, body = _req("POST", f"{base}/sessions/s-bad/messages", token="tok",
                        body={"kind": "prompt", "prompt": "gibberish",
                              "strategy": "deterministic", "binding": "pid:42"})
        assert body["accepted"] is False
        assert _req("GET", base + "/bindings/pid:42", token="tok")[1]["session_id"] == "s-new"
    finally:
        srv.shutdown()


def _rpc(proc, msg):
    proc.stdin.write((json.dumps(msg) + "\n").encode())
    proc.stdin.flush()
    if "id" not in msg:
        return None
    line = proc.stdout.readline()
    assert line, proc.stderr.read().decode()
    return json.loads(line)


def test_facade_end_to_end_over_stdio():
    srv = _serve(auth=TokenAuth({"agent": "tok"}))
    proc = None
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        # The prompt hook registers the plan and binds it to the agent process.
        st, body = _req("POST", f"{base}/sessions/sess-1/messages", token="tok",
                        body={"kind": "prompt", "prompt": AURORA,
                              "strategy": "deterministic", "binding": "pid:777"})
        assert body["accepted"], body

        env = {**os.environ, "GATEWAY_URL": base, "GATEWAY_AUTH_TOKEN": "tok",
               "CLAUDE_PID": "777", "PYTHONPATH": str(ROOT)}
        env.pop("PAUTH_SESSION_ID", None)
        proc = subprocess.Popen(
            [sys.executable, "-m", "gateway.serving.mcp_facade"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, cwd=str(ROOT),
        )
        init = _rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                      "clientInfo": {"name": "test", "version": "0"}}})
        assert init["result"]["protocolVersion"] == "2025-03-26"
        assert "tools" in init["result"]["capabilities"]
        _rpc(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert _rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "ping"})["result"] == {}

        listed = _rpc(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        names = {t["name"] for t in listed["result"]["tools"]}
        assert {"get_product_details", "add_to_cart", "send_money"} <= names

        # In-plan call: authorized AND executed by the gateway; the facade returns the value.
        ok = _rpc(proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                         "params": {"name": "get_product_details",
                                    "arguments": {"name": "Aurora Noise Cancelling Headphones"}}})
        assert ok["result"]["isError"] is False, ok
        assert "120" in ok["result"]["content"][0]["text"]

        # Tampered recipient: denied, value-free reason, nothing executed.
        bad = _rpc(proc, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                          "params": {"name": "send_money",
                                     "arguments": {"recipient": "ATTACKER99", "amount": 120.0,
                                                   "subject": "Order payment", "date": "2026-08-22"}}})
        assert bad["result"]["isError"] is True
        assert "denied" in bad["result"]["content"][0]["text"]
        assert "ATTACKER99" not in bad["result"]["content"][0]["text"]

        # Off-plan known tool: denied and HELD for the human.
        held = _rpc(proc, {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                           "params": {"name": "list_products",
                                      "arguments": {"category": "audio", "max_price": 200.0}}})
        assert held["result"]["isError"] is True
        assert "HELD" in held["result"]["content"][0]["text"]
        st, status = _req("GET", f"{base}/sessions/sess-1", token="tok")
        assert status["pending_reauthorizations"] == 1

        unknown = _rpc(proc, {"jsonrpc": "2.0", "id": 7, "method": "resources/list"})
        assert unknown["error"]["code"] == -32601
    finally:
        if proc is not None:
            proc.stdin.close()
            proc.wait(timeout=10)
        srv.shutdown()


def test_facade_without_bound_session_fails_closed():
    srv = _serve(auth=None)
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        facade = Facade(Daemon(base, ""), env={"CLAUDE_PID": "1"})
        resp = facade.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "get_product_details",
                                         "arguments": {"name": "x"}}})
        assert resp["result"]["isError"] is True
        assert "no task session" in resp["result"]["content"][0]["text"]
        assert resolve_session(Daemon(base, ""), {"PAUTH_SESSION_ID": "explicit"}) == "explicit"
        assert resolve_session(Daemon(base, ""), {"CLAUDE_CODE_SESSION_ID": "spawned"}) == "spawned"
    finally:
        srv.shutdown()


def test_local_policy_decisions():
    env = {}
    assert decide("Read", env).action == "allow"
    assert decide("Edit", env).action == "allow"
    assert decide("mcp__pauth__read_text_file", env).action == "allow"
    assert decide("mcp__linear__list_issues", env).action == "deny"
    assert decide("Bash", env).action == "deny"
    assert decide("Bash", {"GATEWAY_BASH_POLICY": "allow"}).action == "allow"
    assert decide("WebFetch", env).action == "deny"
    assert decide("get_product_details", env).action == "forward"
    assert decide("mcp__gw__x", {"GATEWAY_FACADE_NAME": "gw"}).action == "allow"
    assert decide("MyTool", {"GATEWAY_LOCAL_TOOLS": "MyTool"}).action == "allow"
