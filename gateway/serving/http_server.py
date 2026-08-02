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

Authentication
--------------
With ``--auth-token`` (or ``--auth-tokens <principal:token map>``) every route
except ``GET /health`` requires ``Authorization: Bearer <token>``; the token's
principal is bound to the sessions it creates, so no other principal can read,
drive, or delete them (constant-time token compare; ownership survives a restart
via the session store). Without either flag the server runs in OPEN mode and
must be bound to loopback only -- it prints a warning at startup.

Live channels and envelopes are held in process memory. With a session store,
restart rebuilds the plan and restores the durable execution-attempt ledger;
completed or indeterminate calls stay replay-blocked, while envelope-dependent
continuation fails closed because envelopes are not yet restored.

Usage::

    GATEWAY_AUTH_TOKEN=... .venv/bin/python gateway/serving/http_server.py \
        --host 127.0.0.1 --port 8081 --auth-token "$GATEWAY_AUTH_TOKEN"
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import hmac
import json
import os
import re
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

# Headers we never copy through the LLM proxy: hop-by-hop plus framing headers
# (http.client re-derives Content-Length from the body and de-chunks the upstream,
# and we re-frame the response by connection-close).
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
})


class TokenAuth:
    """Bearer-token authentication mapping a presented token to a principal.

    A request must carry ``Authorization: Bearer <token>``; the matching token's
    principal becomes the caller identity, which is bound to the sessions it
    creates (so no other principal can read/inject/delete them). Tokens are
    compared in constant time.
    """

    def __init__(self, principal_tokens: dict[str, str]) -> None:
        # principal -> token. Reject empty tokens so a blank never authenticates.
        self._tokens = {str(p): str(t) for p, t in principal_tokens.items() if t}

    def principal_for(self, auth_header: str | None) -> str | None:
        prefix = "Bearer "
        if not auth_header or not auth_header.startswith(prefix):
            return None
        presented = auth_header[len(prefix):]
        for principal, token in self._tokens.items():
            if hmac.compare_digest(presented, token):
                return principal
        return None

    @classmethod
    def from_config(cls, single_token: str, tokens_file: str) -> "TokenAuth | None":
        """Build from a single ``--auth-token`` and/or a ``{principal: token}`` file."""
        principal_tokens: dict[str, str] = {}
        if tokens_file:
            data = json.loads(Path(tokens_file).read_text())
            if not isinstance(data, dict):
                raise ValueError("auth-tokens file must be a JSON object {principal: token}")
            principal_tokens.update({str(p): str(t) for p, t in data.items()})
        if single_token:
            principal_tokens.setdefault("operator", single_token)
        return cls(principal_tokens) if principal_tokens else None

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


class SessionRestoreError(RuntimeError):
    """Persisted state exists but cannot be restored without replay risk."""


def restore_channel(
    suite_loader: Callable[[str], SuiteSpec],
    store: "SessionStore",
    session_id: str,
    audit_log: "AuditLog | None" = None,
) -> AgentChannel | None:
    """Rebuild a persisted session without resetting its execution ledger.

    Returns a fresh :class:`AgentChannel` with the plan re-established, or
    ``None`` means the session truly is absent. A legacy/malformed ledger or a
    plan-fingerprint mismatch raises :class:`SessionRestoreError`; callers must
    not fall back to a fresh channel in that case.
    """
    entry = store.get(session_id)
    if entry is None:
        return None
    execution_state = entry.get("execution_state")
    if not isinstance(execution_state, dict):
        raise SessionRestoreError(
            "persisted session has no valid execution state; legacy state is quarantined"
        )
    channel = AgentChannel(
        suite_loader,
        audit_log=audit_log,
        restored_execution_state=execution_state,
        execution_state_sink=lambda state: store.update_execution_state(
            session_id, state
        ),
    )
    message = dict(entry.get("config", {}) or {})
    message.update({"kind": "prompt", "prompt": entry.get("prompt", "")})
    response = channel.receive_json(message)
    if not response.get("accepted"):
        raise SessionRestoreError(
            f"persisted session plan could not be restored: {response.get('reason', '')}"
        )
    return channel


class _Handler(BaseHTTPRequestHandler):
    sessions: dict[str, AgentChannel] = {}
    session_owners: dict[str, str] = {}  # session_id -> authenticated principal
    _lock = threading.Lock()             # guards the session tables (threaded server)
    # Serialize the full lookup -> restore/create -> receive -> persist transition
    # for one session without blocking unrelated sessions. A fixed stripe table
    # avoids an unbounded attacker-controlled lock map.
    _session_locks = tuple(threading.RLock() for _ in range(64))
    max_sessions: int = 10_000           # cap the table; evict oldest (FIFO) beyond it
    # staticmethod: a plain function stored as a class attribute would bind to
    # the handler instance (self.suite_loader -> loader(self, name), 2 args).
    suite_loader: Callable[[str], SuiteSpec] = staticmethod(default_suite_loader)
    session_store: "SessionStore | None" = None  # call interception: opt-in persistence (None = off)
    audit_log: "AuditLog | None" = None  # opt-in shared persistent audit trail
    auth: "TokenAuth | None" = None  # None = open mode (loopback only; warned)
    # Optional LLM proxy: when set, /v1/* is forwarded to this upstream so the
    # agent's LLM traffic and its tool calls share the gateway as their ONLY
    # egress. The network rule then stays "destination = gateway", which never
    # inspects TLS/SNI and so is immune to Encrypted Client Hello (ECH).
    llm_upstream: "str | None" = None  # e.g. "https://api.anthropic.com"; None disables it
    max_proxy_bytes: int = 32 * 1024 * 1024  # LLM prompts can be large
    proxy_timeout: int = 600  # seconds; streaming completions run long

    def log_message(self, fmt: str, *args) -> None:  # noqa: D401 -- quieter logs
        sys.stderr.write("[gateway-http] " + (fmt % args) + "\n")

    # ------------------------------------------------------------------
    # auth + session ownership
    # ------------------------------------------------------------------
    def _authenticate(self) -> str | None:
        """Return the caller's principal, or send 401 and return None.

        In open mode (no auth configured) every caller is the shared ``local``
        principal; the deployment is expected to be loopback-only (warned at
        startup). With auth configured, a valid Bearer token is required.
        """
        if self.auth is None:
            return "local"
        principal = self.auth.principal_for(self.headers.get("Authorization"))
        if principal is None:
            self.send_response(401)
            self.send_header("WWW-Authenticate", "Bearer")
            self.send_header("Content-Length", "0")
            self.end_headers()
        return principal

    def _owner_of(self, session_id: str) -> str | None:
        """The principal that owns ``session_id`` (in memory or persisted), or None."""
        if session_id in self.session_owners:
            return self.session_owners[session_id]
        if self.session_store is not None:
            entry = self.session_store.get(session_id)
            if entry is not None:
                return entry.get("owner")
        return None

    @classmethod
    def _session_lock_for(cls, session_id: str) -> threading.RLock:
        digest = hashlib.sha256(session_id.encode("utf-8")).digest()
        return cls._session_locks[int.from_bytes(digest[:2], "big") % len(cls._session_locks)]

    def _new_channel(self, session_id: str) -> AgentChannel:
        sink = None
        if self.session_store is not None:
            sink = lambda state: self.session_store.update_execution_state(
                session_id, state
            )
        return AgentChannel(
            self.suite_loader,
            audit_log=self.audit_log,
            execution_state_sink=sink,
        )

    @classmethod
    def _add_session(cls, session_id: str, channel: AgentChannel, principal: str) -> None:
        """Insert a session, evicting the oldest if the table is at capacity."""
        with cls._lock:
            while len(cls.sessions) >= cls.max_sessions and session_id not in cls.sessions:
                oldest = next(iter(cls.sessions))
                cls.sessions.pop(oldest, None)
                cls.session_owners.pop(oldest, None)
            cls.sessions[session_id] = channel
            cls.session_owners[session_id] = principal

    # ------------------------------------------------------------------
    # GET -- health / status (value-free; safe on unauthenticated localhost)
    # ------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler API
        if self.llm_upstream is not None and self.path.startswith("/v1/"):
            self._proxy_llm("GET")
            return
        if self.path == "/health":  # liveness only; value-free, no session data
            self._send_json(200, {
                "status": "ok",
                "sessions": len(self.sessions),
                "auth": self.auth is not None,
                "session_store": self.session_store is not None,
                "audit_persisted": self.audit_log is not None,
            })
            return
        principal = self._authenticate()
        if principal is None:
            return
        m = _SESSION_DELETE_RE.match(self.path)  # GET /sessions/<id> -> status
        if m:
            session_id = m.group(1)
            with self._session_lock_for(session_id):
                channel = self.sessions.get(session_id)
                # 404 (not 403) for missing OR not-owned: don't reveal that a session
                # you don't own exists (IDOR/enumeration).
                if channel is None or self.session_owners.get(session_id) != principal:
                    payload = {"error": "no such session", "session_id": session_id}
                    status = 404
                else:
                    payload = {"session_id": session_id, **channel.status()}
                    status = 200
            self._send_json(status, payload)
            return
        self._send_json(404, {"error": f"no route for GET {self.path}"})

    # ------------------------------------------------------------------
    # POST
    # ------------------------------------------------------------------
    max_body_bytes: int = 1 * 1024 * 1024  # cap request body to blunt memory DoS

    def do_POST(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler API
        if self.llm_upstream is not None and self.path.startswith("/v1/"):
            self._proxy_llm("POST")
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            self._send_json(400, {"error": "invalid Content-Length"})
            return
        if length > self.max_body_bytes:
            self._send_json(413, {"error": "request body too large"})
            return
        body = self.rfile.read(length) if length else b""
        principal = self._authenticate()
        if principal is None:
            return
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError as exc:
            self._send_json(400, {"error": f"invalid JSON: {exc}"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "request JSON must be an object"})
            return

        if self.path == "/sessions":
            session_id = str(uuid.uuid4())
            self._add_session(
                session_id,
                self._new_channel(session_id),
                principal,
            )
            self._send_json(201, {"session_id": session_id})
            return

        m = _SESSION_RE.match(self.path)
        if m:
            session_id = m.group(1)
            with self._session_lock_for(session_id):
                status, response = self._handle_session_message(
                    session_id, principal, payload
                )
            self._send_json(status, response)
            return

        self._send_json(404, {"error": f"no route for POST {self.path}"})

    def _handle_session_message(
        self,
        session_id: str,
        principal: str,
        payload: dict,
    ) -> tuple[int, dict]:
        """Handle one session transition while its stripe lock is held."""
        channel = self.sessions.get(session_id)
        if channel is not None:
            if self.session_owners.get(session_id) != principal:
                return 403, {"error": "session belongs to another principal"}
        else:
            owner = self._owner_of(session_id)
            if owner is not None and owner != principal:
                return 403, {"error": "session belongs to another principal"}
            if self.session_store is not None:
                try:
                    channel = restore_channel(
                        self.suite_loader,
                        self.session_store,
                        session_id,
                        audit_log=self.audit_log,
                    )
                except SessionRestoreError as exc:
                    return 409, {
                        "error": "persisted session restore refused (fail-closed)",
                        "detail": str(exc),
                    }
            if channel is None:
                channel = self._new_channel(session_id)
            self._add_session(session_id, channel, principal)

        response = channel.receive_json(payload)
        if (
            self.session_store is not None
            and payload.get("kind") == "prompt"
            and response.get("accepted")
        ):
            config = {
                key: value
                for key, value in payload.items()
                if key not in ("kind", "prompt", "cache_dir")
            }
            try:
                execution_state = channel.execution_state()
                if not isinstance(execution_state, dict):
                    raise RuntimeError("accepted plan has no execution state")
                # Prompt/config/owner and the empty initial ledger are published
                # together. A missing ledger is never interpreted as fresh.
                self.session_store.record(
                    session_id,
                    payload.get("prompt", ""),
                    config,
                    owner=principal,
                    execution_state=execution_state,
                )
            except Exception as exc:  # noqa: BLE001 -- do not expose an unpersisted session
                with self._lock:
                    self.sessions.pop(session_id, None)
                    self.session_owners.pop(session_id, None)
                return 503, {
                    "error": "accepted session could not be durably recorded",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
        return 200, response

    # ------------------------------------------------------------------
    # DELETE
    # ------------------------------------------------------------------
    def do_DELETE(self) -> None:  # noqa: N802
        m = _SESSION_DELETE_RE.match(self.path)
        if not m:
            self._send_json(404, {"error": f"no route for DELETE {self.path}"})
            return
        principal = self._authenticate()
        if principal is None:
            return
        session_id = m.group(1)
        with self._session_lock_for(session_id):
            # 404 for missing OR not-owned (never delete or reveal another's session).
            if self._owner_of(session_id) != principal:
                self._send_json(404, {"deleted": False})
                return
            with self._lock:
                existed = self.sessions.pop(session_id, None) is not None
                self.session_owners.pop(session_id, None)
            if self.session_store is not None:
                self.session_store.remove(session_id)
                existed = True
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

    def _proxy_llm(self, method: str) -> None:
        """Forward a ``/v1/*`` LLM request to ``llm_upstream`` and stream the reply.

        DELIBERATELY exempt from the gateway Bearer token: the client
        authenticates to the *upstream* with its own API key, which we forward
        verbatim. Making the gateway the agent's single egress -- LLM completions
        AND tool-call authorization behind one destination -- lets the network
        rule stay "destination = gateway only", so it never inspects TLS/SNI and
        is unaffected by Encrypted Client Hello (ECH). The agent can reach nothing
        but the gateway, so it cannot bypass it.
        """
        up = urlsplit(self.llm_upstream)
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            self._send_json(400, {"error": "invalid Content-Length"})
            return
        if length > self.max_proxy_bytes:
            self._send_json(413, {"error": "request body too large"})
            return
        raw = self.rfile.read(length) if length else b""
        # Keep x-api-key / authorization / anthropic-* so the client authenticates
        # to the upstream; drop hop-by-hop + framing headers (see _HOP_BY_HOP).
        fwd = {k: v for k, v in self.headers.items() if k.lower() not in _HOP_BY_HOP}
        fwd["Host"] = up.netloc
        port = up.port or (443 if up.scheme == "https" else 80)
        conn_cls = (http.client.HTTPSConnection if up.scheme == "https"
                    else http.client.HTTPConnection)
        try:
            conn = conn_cls(up.hostname, port, timeout=self.proxy_timeout)
            conn.request(method, self.path, body=raw, headers=fwd)
            resp = conn.getresponse()
        except Exception as exc:  # noqa: BLE001 -- upstream unreachable / TLS error
            self._send_json(502, {"error": f"llm upstream error: {type(exc).__name__}: {exc}"})
            return
        try:
            self.send_response(resp.status)
            for key, value in resp.getheaders():
                if key.lower() not in _HOP_BY_HOP:
                    self.send_header(key, value)
            # Frame by connection-close so streaming (SSE, no Content-Length) and
            # buffered replies are handled uniformly; http.client already de-chunks
            # the upstream, so we relay decoded bytes as they arrive.
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception:  # noqa: BLE001 -- client hung up mid-stream
            pass
        finally:
            conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Minimal HTTP wrapper for AgentChannel.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument(
        "--config", default="",
        help=(
            "path to a JSON config (see gateway/serving/config.py); "
            "if omitted, the shopping-only default is used"
        ),
    )
    parser.add_argument(
        "--session-store", default=os.environ.get("SESSION_STORE_PATH", ""),
        help="JSON file to persist sessions across restarts (call interception); empty = disabled",
    )
    parser.add_argument(
        "--audit-log", default=os.environ.get("AUDIT_LOG_PATH", ""),
        help="JSONL file to append operator-facing audit events to; empty = in-memory only",
    )
    parser.add_argument(
        "--auth-token", default=os.environ.get("GATEWAY_AUTH_TOKEN", ""),
        help="shared Bearer token required on every request (principal 'operator'); empty = open mode",
    )
    parser.add_argument(
        "--auth-tokens", default=os.environ.get("GATEWAY_AUTH_TOKENS", ""),
        help="path to a JSON {principal: token} map for per-principal Bearer auth",
    )
    parser.add_argument(
        "--llm-upstream", default=os.environ.get("PAUTH_LLM_UPSTREAM", ""),
        help="if set (e.g. https://api.anthropic.com), proxy /v1/* to this upstream "
             "so the agent's LLM traffic and tool calls share the gateway as their "
             "ONLY egress (network rule stays destination=gateway, ECH-immune); "
             "empty = LLM proxy disabled",
    )
    args = parser.parse_args()

    _Handler.auth = TokenAuth.from_config(args.auth_token, args.auth_tokens)
    if _Handler.auth is None:
        print(
            "WARNING: no auth configured (--auth-token/--auth-tokens). The gateway "
            "is UNAUTHENTICATED -- bind it to loopback only. Any client that can "
            "reach the socket controls it.",
            file=sys.stderr,
        )
    else:
        print("auth: Bearer token required on all routes; sessions bound to principal", file=sys.stderr)

    _Handler.llm_upstream = args.llm_upstream or None
    if _Handler.llm_upstream:
        print(
            f"llm proxy: /v1/* -> {_Handler.llm_upstream} (exempt from the gateway "
            "token; the client's own API key is forwarded). Point the agent's "
            "ANTHROPIC_BASE_URL here so LLM + tool calls share one egress.",
            file=sys.stderr,
        )

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

    # Threaded so one slow request (e.g. an LLM planning call) cannot wedge the
    # whole daemon; the session tables are guarded by a lock.
    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    print(f"gateway-http listening on http://{args.host}:{args.port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("shutting down", file=sys.stderr)
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
