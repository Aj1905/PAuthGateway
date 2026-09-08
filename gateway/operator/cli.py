"""Terminal front-end for the human operator: watch holds and decide them.

The gateway holds two kinds of tool call for a human:

* **reauthorization** -- the agent asked for a known tool that is *not* in the
  plan (default-deny). Approving grants exactly one retry of that exact call.
* **confirmation** -- a plan-internal side effect whose control operand came
  from an untrusted source (tool output). Approving whitelists that value.

This command polls the operator surface of ``gateway/serving/http_server.py``
and asks for a decision per hold. It is the only place operand values are
shown; the agent never sees them.

Usage::

    .venv/bin/python -m gateway.operator.cli --url http://127.0.0.1:8081 \
        --operator-token "$GATEWAY_OPERATOR_TOKEN"            # interactive
    .venv/bin/python -m gateway.operator.cli ... --once --list  # print and exit
    .venv/bin/python -m gateway.operator.cli ... --decide <session>:<kind>:<id>:approve
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any


class OperatorClient:
    def __init__(self, url: str, token: str) -> None:
        self._url = url.rstrip("/")
        self._token = token

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self._url + path,
            data=data,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return exc.code, {"error": raw.decode("utf-8", "replace")}

    def sessions(self) -> list[dict[str, Any]]:
        status, body = self._request("GET", "/sessions")
        if status != 200:
            raise RuntimeError(f"GET /sessions -> {status}: {body}")
        return body.get("sessions", [])

    def pending(self, session_id: str) -> dict[str, Any]:
        status, body = self._request("GET", f"/sessions/{session_id}/pending")
        if status != 200:
            raise RuntimeError(f"GET /sessions/{session_id}/pending -> {status}: {body}")
        return body

    def decide(self, session_id: str, kind: str, hold_id: str, approved: bool) -> bool:
        status, body = self._request(
            "POST",
            f"/sessions/{session_id}/decisions",
            {"kind": kind, "id": hold_id, "approved": approved},
        )
        if status not in (200, 409):
            raise RuntimeError(f"POST decisions -> {status}: {body}")
        return bool(body.get("resolved"))


def render_hold(session_id: str, hold: dict[str, Any]) -> str:
    lines = [f"--- session {session_id} :: {hold['kind']} {hold['id']} ---"]
    if hold["kind"] == "reauthorization":
        lines.append(f"【何をするタスク】計画外のツール呼び出し: {hold['tool']}")
        lines.append("【オペランド】")
        for name, value in hold.get("operands", {}).items():
            lines.append(f"  {name} = {value!r}")
        lines.append(
            "【参照情報】この呼び出しは承認された計画に無い。承認すると、この値の"
            "この呼び出しが一回だけ許可される(計画は変わらない)。"
        )
    else:
        lines.append(hold.get("display", ""))
        warning = hold.get("warning", "")
        if warning:
            lines.append(f"【注意】{warning}")
    return "\n".join(lines)


def _all_holds(client: OperatorClient) -> list[tuple[str, dict[str, Any]]]:
    holds: list[tuple[str, dict[str, Any]]] = []
    for session in client.sessions():
        if not (session.get("pending_confirmations") or session.get("pending_reauthorizations")):
            continue
        pending = client.pending(session["session_id"])
        for hold in pending.get("reauthorizations", []) + pending.get("confirmations", []):
            holds.append((session["session_id"], hold))
    return holds


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip().lower()
    except EOFError:
        return "q"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default="http://127.0.0.1:8081")
    ap.add_argument("--operator-token", required=True)
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    ap.add_argument("--list", action="store_true", help="print holds; do not ask")
    ap.add_argument("--interval", type=float, default=2.0, help="poll seconds")
    ap.add_argument(
        "--decide", action="append", default=[],
        help="non-interactive decision <session>:<kind>:<id>:<approve|reject>",
    )
    args = ap.parse_args(argv)
    client = OperatorClient(args.url, args.operator_token)

    if args.decide:
        rc = 0
        for spec in args.decide:
            try:
                session_id, kind, hold_id, verdict = spec.split(":", 3)
            except ValueError:
                print(f"bad --decide {spec!r}", file=sys.stderr)
                return 2
            resolved = client.decide(session_id, kind, hold_id, verdict == "approve")
            print(json.dumps({"session": session_id, "kind": kind, "id": hold_id,
                              "approved": verdict == "approve", "resolved": resolved}))
            rc = rc or (0 if resolved else 1)
        return rc

    seen: set[tuple[str, str, str]] = set()
    while True:
        holds = _all_holds(client)
        if args.list:
            for session_id, hold in holds:
                print(render_hold(session_id, hold))
            if not holds:
                print("(no holds)")
            if args.once:
                return 0
        else:
            for session_id, hold in holds:
                key = (session_id, hold["kind"], hold["id"])
                if key in seen:
                    continue
                print(render_hold(session_id, hold))
                answer = _ask("承認しますか? [y]es / [n]o / [s]kip / [q]uit: ")
                if answer == "q":
                    return 0
                if answer in ("y", "n"):
                    resolved = client.decide(session_id, hold["kind"], hold["id"], answer == "y")
                    print("  ->", "承認" if answer == "y" else "却下", "" if resolved else "(既に解決済み)")
                    seen.add(key)
                elif answer == "s":
                    seen.add(key)
            if args.once:
                return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
