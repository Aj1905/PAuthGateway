"""Minimal HTTP wrapper for :class:`AgentChannel` (stdlib only).

Wire protocol
-------------
* ``POST /sessions`` -- create a new agent session with a server-generated
  id. Returns ``{"session_id": "<uuid>"}``. Convenient for in-process tests.
* ``POST /sessions/<id>/messages`` -- send an agent message (JSON body
  follows the :mod:`gateway.agent_channel` schema). If the session does
  not exist yet, it is implicitly created with the supplied id. This is
  the entry point Claude Code hooks use (they pass their own session_id).
* ``DELETE /sessions/<id>`` -- discard a session.
* ``GET /health`` -- liveness + config summary (session count, whether the
  session store / audit log persistence are enabled). Value-free.
* ``GET /sessions/<id>`` -- value-free session status for health checks:
  protection level + caveats, whether a plan is active, rule count, pending
  confirmation count. Carries no operand values, so it is safe on the
  unauthenticated localhost surface (unlike the audit log, which is
  operator-facing and may quote values).

Session state is held in process memory; restarting the server drops
every session. For production wrap in a real web framework with proper
auth and persistent state.

Usage::

    .venv/bin/python gateway/http_server.py --host 127.0.0.1 --port 8081
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pauth.suites.base import SuiteSpec
from pauth.suites.shopping import build_suite as build_shopping_suite

from gateway.ingress.agent_channel import AgentChannel
from gateway.runtime.audit import AuditLog
from gateway.serving.session_store import SessionStore
from gateway.serving.config import load_config, suite_loader_for


def default_suite_loader(name: str) -> SuiteSpec:
    if name == "shopping":
        return build_shopping_suite()
    raise ValueError(f"unknown suite {name!r} (http_server demo supports shopping only)")


# Session IDs are client-supplied so external runtimes (Claude Code hooks)
# can route both prompt and tool-call hooks to the same in-process session.
# Accept any non-trivial path-safe string.
_SESSION_RE = re.compile(r"^/sessions/([A-Za-z0-9_\-.]{1,128})/messages$")
_SESSION_DELETE_RE = re.compile(r"^/sessions/([A-Za-z0-9_\-.]{1,128})$")


def restore_channel(
    suite_loader: Callable[[str], SuiteSpec],
    store: "SessionStore",
    session_id: str,
    audit_log: "AuditLog | None" = None,
) -> AgentChannel | None:
    """Rebuild a persisted session by replaying its stored prompt (B1).

    Returns a fresh :class:`AgentChannel` with the plan re-established, or
    ``None`` if the session is not in the store. Mid-task observations are not
    restored -- the plan is; the client continues from there.
    """
    entry = store.get(session_id)
    if entry is None:
        return None
    channel = AgentChannel(suite_loader, audit_log=audit_log)
    message = {"kind": "prompt", "prompt": entry.get("prompt", "")}
    message.update(entry.get("config", {}) or {})
    channel.receive_json(message)
    return channel


class _Handler(BaseHTTPRequestHandler):
    sessions: dict[str, AgentChannel] = {}
    # staticmethod: a plain function stored as a class attribute would bind to
    # the handler instance (self.suite_loader -> loader(self, name), 2 args).
    suite_loader: Callable[[str], SuiteSpec] = staticmethod(default_suite_loader)
    session_store: "SessionStore | None" = None  # B1: opt-in persistence (None = off)
    audit_log: "AuditLog | None" = None  # opt-in shared persistent audit trail

    def log_message(self, fmt: str, *args) -> None:  # noqa: D401 -- quieter logs
        sys.stderr.write("[gateway-http] " + (fmt % args) + "\n")

    # ------------------------------------------------------------------
    # GET -- health / status (value-free; safe on unauthenticated localhost)
    # ------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler API
        if self.path == "/health":
            self._send_json(200, {
                "status": "ok",
                "sessions": len(self.sessions),
                "session_store": self.session_store is not None,
                "audit_persisted": self.audit_log is not None,
            })
            return
        m = _SESSION_DELETE_RE.match(self.path)  # GET /sessions/<id> -> status
        if m:
            session_id = m.group(1)
            channel = self.sessions.get(session_id)
            if channel is None:
                self._send_json(404, {"error": "no such session", "session_id": session_id})
                return
            self._send_json(200, {"session_id": session_id, **channel.status()})
            return
        self._send_json(404, {"error": f"no route for GET {self.path}"})

    # ------------------------------------------------------------------
    # POST
    # ------------------------------------------------------------------
    max_body_bytes: int = 1 * 1024 * 1024  # cap request body to blunt memory DoS

    def do_POST(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler API
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            self._send_json(400, {"error": "invalid Content-Length"})
            return
        if length > self.max_body_bytes:
            self._send_json(413, {"error": "request body too large"})
            return
        body = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError as exc:
            self._send_json(400, {"error": f"invalid JSON: {exc}"})
            return

        if self.path == "/sessions":
            session_id = str(uuid.uuid4())
            self.sessions[session_id] = AgentChannel(
                self.suite_loader, audit_log=self.audit_log
            )
            self._send_json(201, {"session_id": session_id})
            return

        m = _SESSION_RE.match(self.path)
        if m:
            session_id = m.group(1)
            channel = self.sessions.get(session_id)
            if channel is None:
                # Restore a persisted session after a restart (B1); otherwise
                # create implicitly (the first message under a client-supplied
                # id creates it -- what the Claude Code hooks rely on).
                if self.session_store is not None:
                    channel = restore_channel(
                        self.suite_loader, self.session_store, session_id,
                        audit_log=self.audit_log,
                    )
                if channel is None:
                    channel = AgentChannel(self.suite_loader, audit_log=self.audit_log)
                self.sessions[session_id] = channel
            response = channel.receive_json(payload)
            # Persist an accepted prompt so it survives a restart.
            if (
                self.session_store is not None
                and payload.get("kind") == "prompt"
                and response.get("accepted")
            ):
                config = {k: v for k, v in payload.items() if k not in ("kind", "prompt")}
                self.session_store.record(session_id, payload.get("prompt", ""), config)
            self._send_json(200, response)
            return

        self._send_json(404, {"error": f"no route for POST {self.path}"})

    # ------------------------------------------------------------------
    # DELETE
    # ------------------------------------------------------------------
    def do_DELETE(self) -> None:  # noqa: N802
        m = _SESSION_DELETE_RE.match(self.path)
        if not m:
            self._send_json(404, {"error": f"no route for DELETE {self.path}"})
            return
        session_id = m.group(1)
        existed = self.sessions.pop(session_id, None) is not None
        if self.session_store is not None:
            self.session_store.remove(session_id)
        self._send_json(200 if existed else 404, {"deleted": existed})

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    parser = argparse.ArgumentParser(description="Minimal HTTP wrapper for AgentChannel.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument(
        "--config", default="",
        help="path to a JSON config (see gateway/config.py); if omitted, the shopping-only default is used",
    )
    parser.add_argument(
        "--session-store", default=os.environ.get("SESSION_STORE_PATH", ""),
        help="JSON file to persist sessions across restarts (B1); empty = disabled",
    )
    parser.add_argument(
        "--audit-log", default=os.environ.get("AUDIT_LOG_PATH", ""),
        help="JSONL file to append operator-facing audit events to; empty = in-memory only",
    )
    args = parser.parse_args()

    if args.session_store:
        _Handler.session_store = SessionStore(args.session_store)
        restored = len(_Handler.session_store)
        print(f"session store: {args.session_store} ({restored} persisted)", file=sys.stderr)

    if args.audit_log:
        _Handler.audit_log = AuditLog(args.audit_log)
        print(f"audit log: {args.audit_log} (JSONL, operator-facing)", file=sys.stderr)

    if args.config:
        loaded = load_config(args.config)
        # staticmethod so instance access does not bind the loader (see class def).
        _Handler.suite_loader = staticmethod(suite_loader_for(loaded))
        print(
            f"loaded config :: merged={loaded.merged_name} "
            f"sources={sorted(loaded.sources)}",
            file=sys.stderr,
        )

    server = HTTPServer((args.host, args.port), _Handler)
    print(f"gateway-http listening on http://{args.host}:{args.port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("shutting down", file=sys.stderr)
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
