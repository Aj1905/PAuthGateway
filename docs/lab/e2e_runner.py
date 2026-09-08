"""Run a task set end to end against a live gateway and score it (K1 harness).

Each task: ``setup`` shell commands (prepare the real MCP backend), a
``prompt`` (submitted once), and ``calls`` with ``expect`` (True = the call
must be permitted and executed, False = it must be denied / not dispatched).

    .venv/bin/python docs/lab/e2e_runner.py --tasks tasks.json --url ... --token ...

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


def _approve_holds(base: str, operator_token: str, session: str) -> int:
    """Approve every hold the operator surface reports; return how many."""
    req = urllib.request.Request(
        f"{base}/sessions/{session}/pending",
        headers={"Authorization": f"Bearer {operator_token}"}, method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        pending = json.loads(resp.read().decode("utf-8"))
    n = 0
    for hold in pending.get("reauthorizations", []) + pending.get("confirmations", []):
        _post(f"{base}/sessions/{session}/decisions", operator_token,
              {"kind": hold["kind"], "id": hold["id"], "approved": True})
        n += 1
    return n


def run_task(base: str, token: str, task: dict, strategy: str, model: str,
             operator_token: str = "") -> dict:
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
        if (
            not permitted and call["expect"] and operator_token
            and res.get("reauthorization_required")
        ):
            # The human path: approve the exact held call once, then retry.
            approved_here = _approve_holds(base, operator_token, session)
            out["approvals"] += approved_here
            status, res = _post(
                f"{base}/sessions/{session}/messages", token,
                {"kind": "tool_call", "tool": call["tool"], "kwargs": call.get("kwargs", {})},
            )
            permitted = bool(res.get("permit"))
        dispatched = res.get("execution_status") not in (None, "not_dispatched")
        matched = permitted == call["expect"] and dispatched == call["expect"]
        ok = ok and matched
        out["calls"].append({
            "tool": call["tool"], "expect": call["expect"], "permit": permitted,
            "execution_status": res.get("execution_status"),
            "held": bool(res.get("reauthorization_required")) or approved_here > 0,
            "approved": approved_here,
            "reason": (res.get("reason") or res.get("error") or "")[:160],
            "ok": matched,
        })
    out["pass"] = ok
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", required=True)
    ap.add_argument("--url", default="http://127.0.0.1:8091")
    ap.add_argument("--token", default="labtoken")
    ap.add_argument("--strategy", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--operator-token", default="",
                    help="if set, held calls that are expected to succeed are approved once (human path)")
    ap.add_argument("--only", default="", help="comma-separated task ids")
    ap.add_argument("--out", default="", help="write per-task JSON lines here")
    args = ap.parse_args()
    with open(args.tasks, encoding="utf-8") as fh:
        tasks = json.load(fh)
    if args.only:
        wanted = set(args.only.split(","))
        tasks = [t for t in tasks if t["id"] in wanted]
    results = []
    sink = open(args.out, "a", encoding="utf-8") if args.out else None
    for task in tasks:
        result = run_task(args.url, args.token, task, args.strategy, args.model,
                          operator_token=args.operator_token)
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
    print(f"\nK1: {passed}/{len(results)} tasks passed ({clean} without any human approval)")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
