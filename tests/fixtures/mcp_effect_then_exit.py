"""MCP fixture that applies a tools/call effect, then exits without replying."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    marker = Path(sys.argv[1])
    for line in sys.stdin:
        request = json.loads(line)
        if request.get("method") == "tools/list":
            response = {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {"tools": []},
            }
            print(json.dumps(response), flush=True)
            continue
        if request.get("method") == "tools/call":
            with marker.open("a", encoding="utf-8") as stream:
                stream.write("effect\n")
                stream.flush()
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
