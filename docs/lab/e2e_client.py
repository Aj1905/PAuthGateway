"""Minimal agent-side client for end-to-end runs against the HTTP daemon.

Usage::

    .venv/bin/python docs/lab/e2e_client.py --url http://127.0.0.1:8091 \
        --token labtoken --prompt "..." --calls calls.json

``calls.json`` is a list of ``{"tool": ..., "kwargs": {...}}`` objects sent in
order after the prompt. Every response is printed as one JSON line so a run can
be pasted into the lab log verbatim.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def _post(url: str, token: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def _get(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}, method="GET"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8091")
    ap.add_argument("--token", default="labtoken")
    ap.add_argument("--session", default="", help="reuse a session id (skip prompt)")
    ap.add_argument("--prompt", default="")
    ap.add_argument("--calls", default="", help="JSON file with a list of tool calls")
    ap.add_argument("--strategy", default="")
    ap.add_argument("--model", default="")
    args = ap.parse_args()

    session = args.session
    if not session:
        status, body = _post(f"{args.url}/sessions", args.token, {})
        if status != 201:
            print(json.dumps({"step": "create_session", "status": status, "body": body}))
            return 1
        session = body["session_id"]
        print(json.dumps({"step": "create_session", "session_id": session}))

    if args.prompt:
        msg = {"kind": "prompt", "prompt": args.prompt}
        if args.strategy:
            msg["strategy"] = args.strategy
        if args.model:
            msg["model"] = args.model
        status, body = _post(f"{args.url}/sessions/{session}/messages", args.token, msg)
        print(json.dumps({"step": "prompt", "status": status, **body}, ensure_ascii=False))
        if not body.get("accepted"):
            return 2

    if args.calls:
        with open(args.calls, encoding="utf-8") as fh:
            calls = json.load(fh)
        for call in calls:
            msg = {"kind": "tool_call", "tool": call["tool"], "kwargs": call.get("kwargs", {})}
            status, body = _post(f"{args.url}/sessions/{session}/messages", args.token, msg)
            out = {"step": "tool_call", "tool": call["tool"], "status": status}
            out.update(body)
            rv = out.get("return_value")
            if isinstance(rv, str) and len(rv) > 300:
                out["return_value"] = rv[:300] + "...(truncated)"
            print(json.dumps(out, ensure_ascii=False))

    print(json.dumps({"step": "session_status", **_get(f"{args.url}/sessions/{session}", args.token)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
