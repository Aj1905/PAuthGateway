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


def run_task(base: str, token: str, task: dict, strategy: str, model: str) -> dict:
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
        "plan_seconds": plan_seconds, "calls": [], "pass": False,
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
        dispatched = res.get("execution_status") not in (None, "not_dispatched")
        matched = permitted == call["expect"] and dispatched == call["expect"]
        ok = ok and matched
        out["calls"].append({
            "tool": call["tool"], "expect": call["expect"], "permit": permitted,
            "execution_status": res.get("execution_status"),
            "held": bool(res.get("reauthorization_required")),
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
        result = run_task(args.url, args.token, task, args.strategy, args.model)
        results.append(result)
        line = json.dumps(result, ensure_ascii=False)
        print(line, flush=True)
        if sink:
            sink.write(line + "\n")
            sink.flush()
    print()
    print(f"{'task':6} {'plan':5} {'rules':5} {'calls ok/all':12} {'sec':5} result")
    for r in results:
        calls_ok = sum(1 for c in r["calls"] if c["ok"])
        print(f"{r['id']:6} {'yes' if r['accepted'] else 'NO':5} {r['rule_count']:5} "
              f"{calls_ok:>2}/{len(r['calls']):<9} {r['plan_seconds']:5} {'PASS' if r['pass'] else 'FAIL'}")
    passed = sum(1 for r in results if r["pass"])
    print(f"\nK1: {passed}/{len(results)} tasks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
