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


class MCPError(RuntimeError):
    """Raised when the MCP server returns an error or a malformed payload."""


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
        self._url = url
        self._ids = itertools.count(1)

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method}
        if params is not None:
            body["params"] = params
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self._url, data=data,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MCPError(f"non-JSON response from {self._url}: {exc}: {raw[:200]!r}") from exc
        if "error" in payload:
            raise MCPError(f"{method} error: {payload['error']}")
        return payload.get("result")

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

    def __init__(self, command: list[str]) -> None:
        if not command:
            raise ValueError("StdioTransport requires a non-empty command")
        self._proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            text=False,
        )
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method}
        if params is not None:
            body["params"] = params
        line = (json.dumps(body) + "\n").encode("utf-8")
        with self._lock:
            if self._proc.stdin is None or self._proc.stdout is None:
                raise MCPError("stdio MCP subprocess is not running")
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
            response_line = self._proc.stdout.readline()
        if not response_line:
            stderr = b""
            if self._proc.stderr is not None:
                try:
                    stderr = self._proc.stderr.read(2048) or b""
                except Exception:  # noqa: BLE001
                    pass
            raise MCPError(
                f"stdio MCP subprocess closed before responding "
                f"({method}); stderr: {stderr.decode('utf-8', 'replace')[:400]!r}"
            )
        try:
            payload = json.loads(response_line.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise MCPError(f"non-JSON line from stdio MCP: {exc}: {response_line!r}") from exc
        if "error" in payload:
            raise MCPError(f"{method} error: {payload['error']}")
        return payload.get("result")

    def close(self) -> None:
        proc = self._proc
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


def _tool_doc_from_mcp(tool: dict[str, Any]) -> tuple[ToolDoc, list[str]]:
    input_schema = tool.get("inputSchema") or {}
    properties = input_schema.get("properties") or {}
    declared_order = list(properties.keys())
    parameters = [
        {
            "name": name,
            "type": _type_from_schema(properties.get(name, {})),
            "desc": (properties.get(name, {}).get("description") or "").strip(),
        }
        for name in declared_order
    ]
    doc = ToolDoc(
        name=tool["name"],
        description=(tool.get("description") or "").strip(),
        parameters=parameters,
        returns="object",
    )
    return doc, declared_order


# --------------------------------------------------------------------------
# Suite construction
# --------------------------------------------------------------------------

def build_mcp_suite_from_transport(
    name: str, transport: _Transport, signer: str | None = None,
) -> SuiteSpec:
    """Build a :class:`SuiteSpec` from an already-constructed transport.

    The transport is owned by the returned suite -- it stays alive for
    the life of the gateway. Callers that need explicit cleanup can
    access it via the suite's runner closure (or, in practice, just
    leave it to process exit).
    """
    signer = signer or name

    result = transport.rpc("tools/list")
    if not result or "tools" not in result:
        raise MCPError(f"tools/list returned no tools: {result!r}")

    tool_specs: dict[str, ToolSpec] = {}
    param_order: dict[str, list[str]] = {}
    for tool_entry in result["tools"]:
        doc, order = _tool_doc_from_mcp(tool_entry)
        tool_specs[tool_entry["name"]] = ToolSpec(
            name=tool_entry["name"],
            params=order,
            doc=doc,
            signer=signer,
        )
        param_order[tool_entry["name"]] = order

    # The env carries the transport so the runner can issue calls.
    def make_env() -> _Transport:
        return transport

    def runner_factory(env: _Transport) -> Callable[[str, dict[str, Any]], Any]:
        def run(tool: str, kwargs: dict[str, Any]) -> Any:
            if tool not in param_order:
                raise ValueError(f"unknown MCP tool {tool!r} on suite {name!r}")
            result = env.rpc("tools/call", {"name": tool, "arguments": dict(kwargs)})
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
        runner_factory=runner_factory,
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
