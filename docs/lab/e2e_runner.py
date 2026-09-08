"""Run a task set end to end against a live gateway and score it (K1 harness).

Each task: ``setup`` shell commands (prepare the real MCP backend), a
``prompt`` (submitted once), and ``calls`` with ``expect`` (True = the call
must be permitted and executed, False = it must be denied / not dispatched).

    .venv/bin/python docs/lab/e2e_runner.py --tasks tasks.json --url ... --token ...

Approval is disabled by default, including when an operator token is supplied.
Use --approval-mode interactive to ask an operator, or --approval-mode oracle
for a benchmark-only automatic decision. Records name the selected mode and
record resolved decisions; an oracle run is not evidence of human review.

Prints one JSON line per task and a summary table. A task PASSES only when
the plan is accepted and every call matches its expectation.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import unicodedata


def _post(url: str, token: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def _decide_holds(base: str, operator_token: str, session: str,
                  approval_mode: str, input_fn=None) -> list[dict]:
    """Resolve holds only in an explicit operator or benchmark-oracle mode."""
    if approval_mode == "none":
        return []
    if approval_mode not in ("interactive", "oracle"):
        raise ValueError("unknown approval mode")
    req = urllib.request.Request(
        f"{base}/sessions/{session}/pending",
        headers={"Authorization": f"Bearer {operator_token}"}, method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        pending = json.loads(resp.read().decode("utf-8"))
    decisions = []
    for hold in pending.get("reauthorizations", []) + pending.get("confirmations", []):
        if approval_mode == "interactive":
            # Keep the prompt off stdout so per-task JSON remains machine-readable.
            print("保留ツール呼び出し（運用者向け）", file=sys.stderr)
            display = json.dumps(hold, ensure_ascii=False, indent=2)
            # JSON escapes terminal controls; escape invisible format controls
            # as well while keeping Japanese provenance readable.
            display = ''.join(f"\\u{ord(c):04x}" if unicodedata.category(c) == 'Cf' else c for c in display)
            print(display, file=sys.stderr)
            print("承認 y / 却下 n / 今回は判断しない s:", file=sys.stderr)
            try:
                answer = (input_fn or input)().strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = "s"
            if answer not in ("y", "n"):
                decisions.append({"id": hold["id"], "kind": hold["kind"],
                                  "source": "interactive", "approved": None,
                                  "resolved": False})
                continue
            approved = answer == "y"
        else:
            approved = True
        status, result = _post(f"{base}/sessions/{session}/decisions", operator_token,
                              {"kind": hold["kind"], "id": hold["id"], "approved": approved})
        decisions.append({"id": hold["id"], "kind": hold["kind"], "source": approval_mode,
                          "approved": approved, "resolved": status == 200 and result.get("resolved") is True})
    return decisions


def run_task(base: str, token: str, task: dict, strategy: str, model: str,
             operator_token: str = "", approval_mode: str = "none", input_fn=None) -> dict:
    if approval_mode not in ("none", "interactive", "oracle"):
        raise ValueError("unknown approval mode")
    if approval_mode != "none" and not operator_token:
        raise ValueError("approval mode requires an operator token")
    for cmd in task.get("setup", []):
        subprocess.run(cmd, shell=True, check=True)
    status, body = _post(f"{base}/sessions", token, {})
    session = body["session_id"]
    msg = {"kind": "prompt", "prompt": task["prompt"]}
    if strategy:
        msg["strategy"] = strategy
    if model:
        msg["model"] = model
    t0 = time.time()
    status, prompt_result = _post(f"{base}/sessions/{session}/messages", token, msg)
    plan_seconds = round(time.time() - t0, 1)
    out = {
        "id": task["id"], "session": session, "accepted": bool(prompt_result.get("accepted")),
        "rule_count": prompt_result.get("rule_count", 0), "plan_reason": prompt_result.get("reason", ""),
        "plan_seconds": plan_seconds, "calls": [], "pass": False, "approvals": 0,
        "approval_mode": approval_mode, "decisions": [],
    }
    if not out["accepted"]:
        return out
    ok = True
    for call in task["calls"]:
        status, res = _post(
            f"{base}/sessions/{session}/messages", token,
            {"kind": "tool_call", "tool": call["tool"], "kwargs": call.get("kwargs", {})},
        )
        permitted = bool(res.get("permit"))
        approved_here = 0
        decisions = []
        held = bool(res.get("reauthorization_required") or res.get("confirmation_required"))
        if not permitted and operator_token and (
            approval_mode == "interactive" or (approval_mode == "oracle" and call["expect"])
        ):
            # Oracle consults the test expectation; interactive does not hide
            # attacked calls from the operator based on ground truth.
            decisions = _decide_holds(base, operator_token, session, approval_mode, input_fn)
            out["decisions"].extend(decisions)
            approved_here = sum(d["approved"] is True and d["resolved"] for d in decisions)
            out["approvals"] += approved_here
            if approved_here:
                status, res = _post(
                    f"{base}/sessions/{session}/messages", token,
                    {"kind": "tool_call", "tool": call["tool"], "kwargs": call.get("kwargs", {})},
                )
                permitted = bool(res.get("permit"))
        # Dispatch is not success: errors and indeterminate outcomes fail K1.
        if call["expect"]:
            matched = permitted and res.get("execution_status") == "succeeded"
        else:
            matched = not permitted and res.get("execution_status") == "not_dispatched"
        ok = ok and matched
        out["calls"].append({
            "tool": call["tool"], "expect": call["expect"], "permit": permitted,
            "execution_status": res.get("execution_status"),
            "held": held or bool(decisions),
            "decisions": decisions,
            "approved": approved_here,
            "reason": (res.get("reason") or res.get("error") or "")[:160],
            "ok": matched,
        })
    out["pass"] = ok
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", required=True)
    ap.add_argument("--url", default="http://127.0.0.1:8091")
    ap.add_argument("--token", default="labtoken")
    ap.add_argument("--strategy", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--operator-token", default="",
                    help="operator credential; does not itself enable approval")
    ap.add_argument("--approval-mode", choices=("none", "interactive", "oracle"), default="none",
                    help="none: no decisions; interactive: ask operator; oracle: benchmark-only automatic approval")
    ap.add_argument("--only", default="", help="comma-separated task ids")
    ap.add_argument("--out", default="", help="write per-task JSON lines here")
    args = ap.parse_args(argv)
    if args.approval_mode != "none" and not args.operator_token:
        ap.error("--approval-mode requires --operator-token")
    if args.approval_mode == "interactive" and not sys.stdin.isatty():
        ap.error("interactive mode requires a terminal for operator decisions")
    with open(args.tasks, encoding="utf-8") as fh:
        tasks = json.load(fh)
    if args.only:
        wanted = set(args.only.split(","))
        tasks = [t for t in tasks if t["id"] in wanted]
    results = []
    sink = open(args.out, "a", encoding="utf-8") if args.out else None
    for task in tasks:
        result = run_task(args.url, args.token, task, args.strategy, args.model,
                          operator_token=args.operator_token, approval_mode=args.approval_mode)
        results.append(result)
        line = json.dumps(result, ensure_ascii=False)
        print(line, flush=True)
        if sink:
            sink.write(line + "\n")
            sink.flush()
    print()
    print(f"{'task':6} {'plan':5} {'rules':5} {'calls ok/all':12} {'sec':5} {'appr':4} result")
    for r in results:
        calls_ok = sum(1 for c in r["calls"] if c["ok"])
        print(f"{r['id']:6} {'yes' if r['accepted'] else 'NO':5} {r['rule_count']:5} "
              f"{calls_ok:>2}/{len(r['calls']):<9} {r['plan_seconds']:5} {r['approvals']:4} "
              f"{'PASS' if r['pass'] else 'FAIL'}")
    passed = sum(1 for r in results if r["pass"])
    clean = sum(1 for r in results if r["pass"] and r["approvals"] == 0)
    print(f"\nK1: {passed}/{len(results)} tasks passed ({clean} without any approval)")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
