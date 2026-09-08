"""MCP facade: present the gateway to an agent as *the* MCP server.

An unmodified agent (Claude Code, Codex, ...) registers this one stdio MCP
server instead of the real ones. The real MCP servers are configured inside
the gateway daemon (``gateway/serving/config.py``) and never reachable by the
agent directly. Every ``tools/call`` therefore passes through the gateway,
which authorizes it against the task plan and executes it itself -- the L3
shape, with no double execution (a ``PreToolUse`` hook alone cannot give
this: the hook only permits, the agent then executes on its own connection).

Session binding
---------------
MCP carries no session id. The prompt hook (``gateway/hooks/submit_prompt.sh``)
sends ``binding: "pid:<CLAUDE_PID>"`` with the prompt; this process, spawned
by the same agent, presents the same key on every call and the daemon returns
the session most recently planned under it. Resolution order:

1. ``PAUTH_SESSION_ID`` (explicit, for tests and custom agents)
2. ``pid:$CLAUDE_PID`` via ``GET /bindings/<key>``
3. ``$CLAUDE_CODE_SESSION_ID`` (the session at spawn time; stale after a new
   conversation in the same process, hence the pid binding first)

Wire
----
Newline-delimited JSON-RPC 2.0 on stdin/stdout, as the reference MCP servers
speak. Logs go to stderr only.

Usage (Claude Code ``.mcp.json``)::

    {"mcpServers": {"pauth": {"command": "/ABS/.venv/bin/python",
      "args": ["-m", "gateway.serving.mcp_facade"],
      "env": {"GATEWAY_URL": "http://127.0.0.1:8081",
              "GATEWAY_AUTH_TOKEN": "..."}}}}
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "pauth-gateway", "version": "0.1.0"}


class Daemon:
    """Thin HTTP client for the gateway daemon."""

    def __init__(self, url: str, token: str) -> None:
        self._url = url.rstrip("/")
        self._token = token

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        req = urllib.request.Request(self._url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return exc.code, {"error": raw.decode("utf-8", "replace")}

    def tools(self) -> list[dict[str, Any]]:
        status, body = self._request("GET", "/tools")
        if status != 200:
            raise RuntimeError(f"GET /tools -> {status}: {body}")
        return body.get("tools", [])

    def bound_session(self, key: str) -> str | None:
        status, body = self._request("GET", f"/bindings/{key}")
        if status == 200:
            return body.get("session_id")
        return None

    def tool_call(self, session_id: str, tool: str, arguments: dict[str, Any]) -> tuple[int, dict]:
        return self._request(
            "POST", f"/sessions/{session_id}/messages",
            {"kind": "tool_call", "tool": tool, "kwargs": arguments},
        )


def resolve_session(daemon: Daemon, env: dict[str, str]) -> str | None:
    explicit = env.get("PAUTH_SESSION_ID")
    if explicit:
        return explicit
    pid = env.get("CLAUDE_PID")
    if pid:
        bound = daemon.bound_session(f"pid:{pid}")
        if bound:
            return bound
    return env.get("CLAUDE_CODE_SESSION_ID") or None


def _text_result(text: str, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _render_return(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(value)


def _complete_arguments(tool: dict[str, Any] | None, arguments: dict[str, Any]) -> dict[str, Any]:
    """The gateway requires every declared parameter; fill omitted optionals with None."""
    if not tool:
        return dict(arguments)
    props = (tool.get("inputSchema") or {}).get("properties") or {}
    complete = {name: arguments.get(name) for name in props}
    complete.update({k: v for k, v in arguments.items() if k not in props})
    return complete


class Facade:
    def __init__(self, daemon: Daemon, env: dict[str, str] | None = None) -> None:
        self._daemon = daemon
        self._env = dict(os.environ if env is None else env)
        self._tools: dict[str, dict[str, Any]] | None = None

    def _tool_table(self) -> dict[str, dict[str, Any]]:
        if self._tools is None:
            self._tools = {t["name"]: t for t in self._daemon.tools()}
        return self._tools

    # -- JSON-RPC dispatch -------------------------------------------------
    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params") or {}
        is_notification = "id" not in message
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                }
            elif method == "notifications/initialized" or (method or "").startswith("notifications/"):
                return None
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": list(self._tool_table().values())}
            elif method == "tools/call":
                result = self._call(params)
            else:
                if is_notification:
                    return None
                return {"jsonrpc": "2.0", "id": msg_id,
                        "error": {"code": -32601, "message": f"method not found: {method}"}}
        except Exception as exc:  # noqa: BLE001 -- never crash the transport
            if is_notification:
                return None
            return {"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}}
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not name or not isinstance(arguments, dict):
            return _text_result("tools/call needs a tool name and an arguments object", True)
        session_id = resolve_session(self._daemon, self._env)
        if not session_id:
            return _text_result(
                "PAuth gateway: no task session is bound to this agent. The user's "
                "prompt has not been registered (is the UserPromptSubmit hook "
                "installed?). No tool can run without a plan.", True,
            )
        tool = self._tool_table().get(name)
        status, body = self._daemon.tool_call(session_id, name, _complete_arguments(tool, arguments))
        if status != 200:
            return _text_result(f"PAuth gateway error ({status}): {body.get('error', body)}", True)
        if body.get("kind") == "error":
            return _text_result(f"PAuth gateway: {body.get('error', '')}", True)
        if body.get("permit"):
            return _text_result(_render_return(body.get("return_value")))
        reason = body.get("reason") or "denied"
        if body.get("reauthorization_required"):
            reason += (
                " This call is HELD for the user's approval. Do not retry it "
                "immediately; continue with the rest of the task, then tell the "
                "user which call is awaiting approval. The user approves it with "
                "the gateway's operator console (python -m gateway.operator.cli); "
                "after approval, one retry of the exact same call is permitted."
            )
        elif "confirm" in reason.lower() or "pending" in reason.lower():
            reason += (
                " The value came from data the gateway does not trust (tool "
                "output), so the user must confirm it in the operator console "
                "before this call runs. Continue with the rest of the task and "
                "report which call is waiting."
            )
        return _text_result(f"PAuth gateway denied this call: {reason}", True)


def serve(stdin, stdout, facade: Facade) -> None:
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            resp = {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "parse error"}}
        else:
            if not isinstance(message, dict):
                continue
            resp = facade.handle(message)
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            stdout.flush()


def main() -> int:
    url = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8081")
    token = os.environ.get("GATEWAY_AUTH_TOKEN", "")
    facade = Facade(Daemon(url, token))
    print(f"[pauth-facade] gateway={url} session_hint="
          f"{'pid:' + os.environ['CLAUDE_PID'] if os.environ.get('CLAUDE_PID') else os.environ.get('PAUTH_SESSION_ID', '?')}",
          file=sys.stderr, flush=True)
    serve(sys.stdin, sys.stdout, facade)
    return 0


if __name__ == "__main__":
    sys.exit(main())
