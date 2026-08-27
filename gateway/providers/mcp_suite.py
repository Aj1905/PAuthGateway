"""``SuiteSpec`` backed by an MCP server.

Adapts the Model Context Protocol's tool surface to PAuth's ``SuiteSpec``
contract so the gateway can enforce per-call authorization on tools
implemented by any compliant MCP server -- Gmail, Linear, Drive,
project-specific MCPs, etc.

Two transports
--------------
* :class:`HTTPTransport` -- JSON-RPC over HTTP POST. Convenient for
  localhost MCP shims and for the in-process mock server used by tests.
* :class:`StdioTransport` -- JSON-RPC over a subprocess' stdin/stdout
  (line-delimited). Matches how the official MCP reference servers ship
  (e.g. ``@modelcontextprotocol/server-filesystem``).

Both transports satisfy the small :class:`_Transport` protocol below; an
adapter for any other transport (websocket, unix socket, ...) only has
to implement ``rpc`` and ``close``.

PAuth-specific notes
--------------------
* Tool signers default to the suite name passed in. Real deployments
  should set the signer to a stable per-server identifier.
* JSON-Schema → :class:`pauth.codegen.ToolDoc` conversion is best
  effort. Operand-level authorization works only as well as the
  rendered schema; complex nested types degrade to ``"any"`` and may
  need refinement when a particular server is integrated. Pair with
  :class:`gateway.policy.PolicyAwareEnforcer` to mark inherently
  free-form operands (search query, message body, ...).
"""

from __future__ import annotations

import itertools
import json
import subprocess
import threading
import urllib.request
from typing import Any, Callable, Protocol

from pauth.codegen import ToolDoc
from pauth.suites.base import SuiteSpec, ToolSpec
from gateway.providers.openapi_suite import _MAX_RESPONSE_BYTES, _SAFE_OPENER, _require_http_url


class MCPError(RuntimeError):
    """Raised when the MCP server returns an error or a malformed payload."""


def _rpc_result(payload: Any, request_id: int, method: str) -> Any:
    """Validate the JSON-RPC response envelope before trusting its result."""
    if not isinstance(payload, dict):
        raise MCPError(f"{method} returned a non-object JSON-RPC response")
    if payload.get("jsonrpc") != "2.0" or payload.get("id") != request_id:
        raise MCPError(f"{method} returned a mismatched JSON-RPC version/id")
    has_result = "result" in payload
    has_error = "error" in payload
    if has_result == has_error:
        raise MCPError(
            f"{method} response must contain exactly one of result or error"
        )
    if has_error:
        raise MCPError(f"{method} error: {payload['error']}")
    return payload["result"]


# --------------------------------------------------------------------------
# Transport protocol
# --------------------------------------------------------------------------

class _Transport(Protocol):
    """JSON-RPC transport used by :func:`build_mcp_suite`."""

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> Any: ...
    def close(self) -> None: ...


# --------------------------------------------------------------------------
# HTTP transport
# --------------------------------------------------------------------------

class HTTPTransport:
    """JSON-RPC over a single HTTP endpoint (POST)."""

    def __init__(self, url: str) -> None:
        # Gate the endpoint scheme/host (config is operator-set, but a bad value
        # or a redirect must not turn this into a file:// read or metadata SSRF).
        _require_http_url(url, "mcp url")
        self._url = url
        self._ids = itertools.count(1)

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        request_id = next(self._ids)
        body = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            body["params"] = params
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self._url, data=data,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with _SAFE_OPENER.open(req, timeout=30) as resp:  # re-validates redirects
            body = resp.read(_MAX_RESPONSE_BYTES + 1)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise MCPError(f"response from {self._url} exceeds size cap")
            raw = body.decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MCPError(f"non-JSON response from {self._url}: {exc}: {raw[:200]!r}") from exc
        return _rpc_result(payload, request_id, method)

    def close(self) -> None:
        return None


# --------------------------------------------------------------------------
# Stdio transport
# --------------------------------------------------------------------------

class StdioTransport:
    """JSON-RPC over a subprocess' stdin/stdout (line-delimited).

    Spawns ``command`` (a list of argv tokens) and exchanges newline-
    framed JSON-RPC messages with it. The reference MCP servers use
    this transport.
    """

    def __init__(
        self,
        command: list[str],
        on_restart: "Callable[[StdioTransport], None] | None" = None,
        max_restarts: int = 3,
    ) -> None:
        """Spawn ``command`` and supervise it (envelope signing).

        A crashed subprocess is respawned on the next ``rpc`` up to
        ``max_restarts`` times (crash-loop guard). ``on_restart`` is invoked
        after each respawn so callers that need an ``initialize`` handshake can
        replay it; it may safely call back into ``rpc`` (the lock is re-entrant).
        """
        if not command:
            raise ValueError("StdioTransport requires a non-empty command")
        self._command = list(command)
        self._on_restart = on_restart
        self._max_restarts = max_restarts
        self._restarts = 0
        self._ids = itertools.count(1)
        self._lock = threading.RLock()
        self._stderr_lock = threading.Lock()
        self._stderr_tail = bytearray()
        self._proc: subprocess.Popen | None = None
        self._spawn()

    def _spawn(self) -> None:
        self._proc = subprocess.Popen(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            text=False,
        )
        if self._proc.stderr is not None:
            threading.Thread(
                target=self._drain_stderr,
                args=(self._proc.stderr,),
                daemon=True,
            ).start()

    def _drain_stderr(self, stream) -> None:
        """Continuously drain stderr so a noisy MCP cannot block on its pipe."""
        try:
            while True:
                chunk = stream.read(2048)
                if not chunk:
                    break
                with self._stderr_lock:
                    self._stderr_tail.extend(chunk)
                    if len(self._stderr_tail) > 8192:
                        del self._stderr_tail[:-8192]
        except (OSError, ValueError):
            return

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def restarts(self) -> int:
        return self._restarts

    def _restart(self) -> None:
        if self._restarts >= self._max_restarts:
            raise MCPError(
                f"stdio MCP subprocess exceeded {self._max_restarts} restarts "
                "(crash loop); giving up"
            )
        try:
            self._close_proc()
        except Exception:  # noqa: BLE001
            pass
        self._spawn()
        self._restarts += 1
        if self._on_restart is not None:
            try:
                self._on_restart(self)
            except Exception as exc:  # noqa: BLE001
                raise MCPError(f"stdio MCP reinit after restart failed: {exc}") from exc

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        request_id = next(self._ids)
        body = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            body["params"] = params
        line = (json.dumps(body) + "\n").encode("utf-8")
        response_line = b""
        with self._lock:
            for _ in range(self._max_restarts + 1):
                if not self.is_alive():
                    self._restart()
                assert self._proc is not None
                if self._proc.stdin is None or self._proc.stdout is None:
                    self._restart()
                    continue
                try:
                    self._proc.stdin.write(line)
                    self._proc.stdin.flush()
                    response_line = self._proc.stdout.readline()
                except (BrokenPipeError, OSError) as exc:
                    if method == "tools/call":
                        # A partial write may already have dispatched the
                        # external effect. Retrying would duplicate it.
                        try:
                            self._restart()
                        except Exception:  # noqa: BLE001 -- preserve uncertainty
                            pass
                        raise MCPError(
                            "indeterminate tools/call outcome: stdio transport "
                            "failed after dispatch may have begun; not retried"
                        ) from exc
                    self._restart()
                    continue
                if response_line:
                    break
                # An empty read means the server closed after receiving the
                # request. Read-only discovery may be retried; an effecting
                # tool call must surface an indeterminate outcome.
                if method == "tools/call":
                    try:
                        self._restart()
                    except Exception:  # noqa: BLE001 -- preserve uncertainty
                        pass
                    raise MCPError(
                        "indeterminate tools/call outcome: subprocess closed "
                        "before responding; not retried"
                    )
                self._restart()
            else:
                stderr = self._read_stderr()
                raise MCPError(
                    f"stdio MCP subprocess closed before responding ({method}) "
                    f"after {self._restarts} restarts; stderr: {stderr[:400]!r}"
                )
        try:
            payload = json.loads(response_line.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise MCPError(f"non-JSON line from stdio MCP: {exc}: {response_line!r}") from exc
        return _rpc_result(payload, request_id, method)

    def _read_stderr(self) -> str:
        with self._stderr_lock:
            return bytes(self._stderr_tail).decode("utf-8", "replace")

    def _close_proc(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except Exception:  # noqa: BLE001
                    pass
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:  # noqa: BLE001
                        pass

    def close(self) -> None:
        self._close_proc()


# --------------------------------------------------------------------------
# Schema translation
# --------------------------------------------------------------------------

def _type_from_schema(schema: dict[str, Any]) -> str:
    if not isinstance(schema, dict):
        return "any"
    t = schema.get("type")
    if t == "string":
        return "string"
    if t == "integer":
        return "integer"
    if t == "number":
        return "number"
    if t == "boolean":
        return "boolean"
    if t == "array":
        items = schema.get("items")
        if isinstance(items, dict):
            return f"list of {_type_from_schema(items)}"
        return "list"
    if t == "object":
        props = schema.get("properties")
        if isinstance(props, dict):
            inner = ", ".join(f"{k}: {_type_from_schema(v)}" for k, v in props.items())
            return f"object {{{inner}}}"
        return "object"
    if isinstance(t, list):
        return "|".join(_type_from_schema({"type": tt}) for tt in t)
    return "any"


def _description(value: Any) -> str:
    """Return schema prose only when the remote value is actually text."""
    return value.strip() if isinstance(value, str) else ""


def _tool_doc_from_mcp(
    tool: dict[str, Any]
) -> tuple[ToolDoc, list[str], frozenset[str]]:
    if (
        not isinstance(tool, dict)
        or not isinstance(tool.get("name"), str)
        or not tool["name"].strip()
    ):
        raise MCPError(f"tools/list contains an invalid tool entry: {tool!r}")
    input_schema = tool.get("inputSchema", {})
    if not isinstance(input_schema, dict):
        raise MCPError(f"MCP tool {tool['name']!r} has a non-object inputSchema")
    properties = input_schema.get("properties", {})
    if not isinstance(properties, dict):
        raise MCPError(f"MCP tool {tool['name']!r} has non-object properties")
    if any(
        not isinstance(property_name, str)
        or not isinstance(property_schema, dict)
        for property_name, property_schema in properties.items()
    ):
        raise MCPError(f"MCP tool {tool['name']!r} has invalid property schemas")
    declared_order = list(properties.keys())
    required_raw = input_schema.get("required", [])
    if (
        not isinstance(required_raw, list)
        or any(not isinstance(item, str) for item in required_raw)
        or not set(required_raw).issubset(properties)
    ):
        raise MCPError(f"MCP tool {tool['name']!r} has an invalid required list")
    required = frozenset(required_raw)
    parameters = [
        {
            "name": name,
            "type": _type_from_schema(properties.get(name, {})),
            "desc": (
                _description(properties.get(name, {}).get("description"))
                + ("" if name in required else " (optional; pass None to omit)")
            ).strip(),
        }
        for name in declared_order
    ]
    doc = ToolDoc(
        name=tool["name"],
        description=_description(tool.get("description")),
        parameters=parameters,
        returns="object",
    )
    return doc, declared_order, required


# --------------------------------------------------------------------------
# Suite construction
# --------------------------------------------------------------------------

def build_mcp_suite_from_transport(
    name: str, transport: _Transport, signer: str | None = None,
) -> SuiteSpec:
    """Build a :class:`SuiteSpec` from an already-constructed transport.

    The transport is owned by the returned suite -- it stays alive for
    the life of the gateway. Callers that need explicit cleanup can
    access it via the suite's tool_executor closure (or, in practice, just
    leave it to process exit).
    """
    signer = signer or name

    result = transport.rpc("tools/list")
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("tools"), list)
        or not result["tools"]
    ):
        raise MCPError(f"tools/list returned no tools: {result!r}")

    tool_specs: dict[str, ToolSpec] = {}
    param_order: dict[str, list[str]] = {}
    required_params: dict[str, frozenset[str]] = {}
    for tool_entry in result["tools"]:
        doc, order, required = _tool_doc_from_mcp(tool_entry)
        if doc.name in tool_specs:
            raise MCPError(f"tools/list returned duplicate tool name {doc.name!r}")
        tool_specs[tool_entry["name"]] = ToolSpec(
            name=tool_entry["name"],
            params=order,
            doc=doc,
            signer=signer,
        )
        param_order[tool_entry["name"]] = order
        required_params[tool_entry["name"]] = required

    # The env carries the transport so the tool_executor can issue calls.
    def make_env() -> _Transport:
        return transport

    def tool_executor_factory(env: _Transport) -> Callable[[str, dict[str, Any]], Any]:
        def run(tool: str, kwargs: dict[str, Any]) -> Any:
            if tool not in param_order:
                raise ValueError(f"unknown MCP tool {tool!r} on suite {name!r}")
            arguments = {
                key: value
                for key, value in kwargs.items()
                if value is not None or key in required_params[tool]
            }
            result = env.rpc(
                "tools/call", {"name": tool, "arguments": arguments}
            )
            if isinstance(result, dict) and result.get("isError") is True:
                raise MCPError(
                    f"MCP tool {tool!r} reported an application error"
                )
            if isinstance(result, dict) and "content" in result and isinstance(result["content"], list):
                texts = [
                    c.get("text") for c in result["content"]
                    if isinstance(c, dict) and c.get("type") == "text"
                ]
                if len(texts) == 1:
                    try:
                        return json.loads(texts[0])
                    except (TypeError, json.JSONDecodeError):
                        return texts[0]
                return result["content"]
            return result

        return run

    return SuiteSpec(
        name=name,
        tools=tool_specs,
        make_env=make_env,
        tool_executor_factory=tool_executor_factory,
        tasks=[],
    )


def build_mcp_suite(name: str, url: str, signer: str | None = None) -> SuiteSpec:
    """Convenience: HTTP transport + suite construction in one call."""
    return build_mcp_suite_from_transport(name, HTTPTransport(url), signer=signer)


def build_mcp_suite_stdio(
    name: str, command: list[str], signer: str | None = None,
) -> SuiteSpec:
    """Convenience: stdio transport + suite construction in one call."""
    return build_mcp_suite_from_transport(name, StdioTransport(command), signer=signer)
