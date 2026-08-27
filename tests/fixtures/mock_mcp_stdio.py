"""Stdio MCP server fixture.

Same surface as ``mock_mcp_server.py`` (the shopping suite wrapped) but
speaking JSON-RPC over stdin/stdout, one message per line. Used by the
``StdioTransport`` integration test.

Run directly for ad-hoc testing::

    .venv/bin/python tests/fixtures/mock_mcp_stdio.py

It will read JSON-RPC requests on stdin and write responses on stdout.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.fixtures.mock_mcp_server import _tools_call, _tools_list  # reuse logic


def _handle(payload: dict[str, Any]) -> dict[str, Any]:
    rpc_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}
    try:
        if method == "tools/list":
            result = _tools_list()
        elif method == "noisy":
            sys.stderr.write("x" * 200_000)
            sys.stderr.flush()
            result = {"ok": True}
        elif method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(name, str):
                return {"jsonrpc": "2.0", "id": rpc_id,
                        "error": {"code": -32602, "message": "tools/call missing 'name'"}}
            result = _tools_call(name, arguments)
        else:
            return {"jsonrpc": "2.0", "id": rpc_id,
                    "error": {"code": -32601, "message": f"method not implemented: {method!r}"}}
    except Exception as exc:  # noqa: BLE001
        return {"jsonrpc": "2.0", "id": rpc_id,
                "error": {"code": -32000, "message": f"{type(exc).__name__}: {exc}"}}
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def main() -> int:
    for line in sys.stdin.buffer:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as exc:
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": f"parse error: {exc}"},
            }) + "\n")
            sys.stdout.flush()
            continue
        response = _handle(payload)
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
