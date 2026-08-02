"""Minimal MCP-over-HTTP server wrapping the shopping suite.

Used by ``gateway/providers/mcp_suite.py`` integration tests so we can drive the
``MCPSuite`` end-to-end without depending on a real third-party MCP
server. Only the two RPCs PAuth needs are implemented:

* ``tools/list`` -- returns the shopping suite's tools with
  JSON-schema-ish ``inputSchema`` blocks.
* ``tools/call`` -- routes the call to the in-process shopping tool_executor.

Run::

    .venv/bin/python tests/fixtures/mock_mcp_server.py --port 8090

then point an MCPSuite at ``http://127.0.0.1:8090/``.
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pauth.suites.shopping import build_suite as build_shopping_suite


# --------------------------------------------------------------------------
# Translate the shopping suite's ToolDoc into a JSON-Schema-ish shape.
# --------------------------------------------------------------------------

_TYPE_TO_JSONSCHEMA = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
}


def _doc_to_input_schema(doc) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for p in doc.parameters:
        t = p.get("type", "string")
        # Best-effort scalar/array/object mapping; the shopping schema is
        # simple enough that we don't need to be clever here.
        if t.startswith("list"):
            properties[p["name"]] = {"type": "array"}
        elif t.startswith("object"):
            properties[p["name"]] = {"type": "object"}
        else:
            properties[p["name"]] = {"type": _TYPE_TO_JSONSCHEMA.get(t, "string")}
        if p.get("desc"):
            properties[p["name"]]["description"] = p["desc"]
        required.append(p["name"])
    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


_SUITE = build_shopping_suite()
_ENV = _SUITE.make_env()
_RUNNER = _SUITE.tool_executor_factory(_ENV)


def _tools_list() -> dict[str, Any]:
    tools = []
    for spec in _SUITE.tools.values():
        tools.append({
            "name": spec.name,
            "description": spec.doc.description,
            "inputSchema": _doc_to_input_schema(spec.doc),
        })
    return {"tools": tools}


def _tools_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = _RUNNER(name, arguments)
    # MCP wants serialisable content blocks; we wrap the raw result as a
    # single text block carrying its JSON repr.
    return {
        "content": [{"type": "text", "text": _to_json_text(result)}],
        "isError": False,
    }


def _to_json_text(value: Any) -> str:
    """Best-effort serialise a shopping suite return for the text envelope."""
    try:
        return json.dumps(value, default=_default_serialiser)
    except Exception:  # noqa: BLE001
        return repr(value)


def _default_serialiser(value: Any) -> Any:
    if hasattr(value, "__dict__"):
        return value.__dict__
    return repr(value)


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # noqa: D401
        sys.stderr.write("[mock-mcp] " + (fmt % args) + "\n")

    def do_POST(self) -> None:  # noqa: N802 -- API name
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError as exc:
            self._jsonrpc_error(None, -32700, f"parse error: {exc}")
            return

        rpc_id = payload.get("id")
        method = payload.get("method")
        params = payload.get("params") or {}

        try:
            if method == "tools/list":
                result = _tools_list()
            elif method == "tools/call":
                name = params.get("name")
                arguments = params.get("arguments") or {}
                if not isinstance(name, str):
                    return self._jsonrpc_error(rpc_id, -32602, "tools/call missing 'name'")
                result = _tools_call(name, arguments)
            else:
                return self._jsonrpc_error(rpc_id, -32601, f"method not implemented: {method!r}")
        except Exception as exc:  # noqa: BLE001
            return self._jsonrpc_error(rpc_id, -32000, f"{type(exc).__name__}: {exc}")

        self._jsonrpc_ok(rpc_id, result)

    def _jsonrpc_ok(self, rpc_id: Any, result: Any) -> None:
        body = json.dumps({"jsonrpc": "2.0", "id": rpc_id, "result": result}).encode("utf-8")
        self._respond(200, body)

    def _jsonrpc_error(self, rpc_id: Any, code: int, message: str) -> None:
        body = json.dumps({
            "jsonrpc": "2.0", "id": rpc_id,
            "error": {"code": code, "message": message},
        }).encode("utf-8")
        self._respond(200, body)

    def _respond(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()

    server = HTTPServer((args.host, args.port), _Handler)
    print(f"mock-mcp listening on http://{args.host}:{args.port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("shutting down", file=sys.stderr)
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
