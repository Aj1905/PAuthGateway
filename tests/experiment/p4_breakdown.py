"""E0 — per-task breakdown of MISSING and EXCESS calls for cached best-of candidates.

Re-scores a funnel scratch tag (default: the adopted ``struct_p4_gpt-5_1``)
with the funnel's own selection (clean, then most side-effecting) and the
shared control-operand matcher, but returns the *lists* of unmatched calls
instead of counts, so the residual failures can be grouped into shapes that a
generic planning rule could address. No API calls.

Usage: .venv/bin/python -m tests.experiment.p4_breakdown [--tag struct_p4_gpt-5_1_] [--json out.json]
"""
from __future__ import annotations

import argparse
import copy
import json
from collections import Counter
from pathlib import Path

from agentdojo.task_suite.load_suites import get_suites

from benchmarks.agentdojo_adapter import load_suite
from benchmarks.structured_read import augment_with_structuring
from eval.gates import GATEWAY_INTERNAL_TOOLS, _in_pool, _norm, _positional
from gateway.planning.prechecks import PrecheckPolicy
from gateway.runtime.confirmation import control_operands, is_side_effecting
from pauth import prepare
from pauth.enforcer import Enforcer
from pauth.envelope import EnvelopeStore, KeyRing
from pauth.grammar_validator import DSLRejectionError
from pauth.tool_executor import execute_generated_code

SCRATCH = Path("tests/experiment/funnel_scratch")
SUITES = ["banking", "slack", "travel", "workspace"]


def _exec(suite, code):
    try:
        prepared = prepare(code, suite.tool_names(), suite.tool_signer())
    except DSLRejectionError as exc:
        return None, False, [], f"dsl: {exc}"
    enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), suite.tool_signer())
    rep = execute_generated_code(prepared.source, enf, suite.tool_params(),
                                 suite.tool_executor_factory(suite.make_env()))
    trace = [(e.tool, list(e.args)) for e in rep.events if e.decision.permit]
    clean = rep.crashed is None and not rep.denied
    return prepared, clean, trace, (rep.crashed or ("denied" if rep.denied else ""))


def _match_lists(reference, observed, docs):
    """One-to-one control-operand matching; return (excess_calls, missing_calls)."""
    policy = PrecheckPolicy()

    def matches(call, ref_call):
        tool, args = call
        ref_tool, ref_args = ref_call
        if tool != ref_tool:
            return False
        indices = [i for i, _ in control_operands(tool, docs, policy)]
        return all(
            i < len(args) and i < len(ref_args)
            and (_norm(args[i]) == _norm(ref_args[i]) or _in_pool(args[i], {_norm(ref_args[i])}))
            for i in indices
        )

    matched: set[int] = set()
    excess = []
    for call in observed:
        hit = next((i for i, r in enumerate(reference) if i not in matched and matches(call, r)), None)
        if hit is None:
            excess.append(call)
        else:
            matched.add(hit)
    missing = [r for i, r in enumerate(reference) if i not in matched]
    return excess, missing


def _shape(tool: str, docs) -> str:
    return "write" if is_side_effecting(tool) else "read"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="struct_p4_gpt-5_1_")
    ap.add_argument("--json", default="")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    rows = []
    miss_by_tool: Counter = Counter()
    exc_by_tool: Counter = Counter()
    miss_kind: Counter = Counter()
    exc_kind: Counter = Counter()
    totals = Counter()
    for sname in SUITES:
        adj = get_suites("v1")[sname]
        suite = augment_with_structuring(load_suite(sname))
        docs = {n: s.doc for n, s in suite.tools.items()}
        params = suite.tool_params()
        base = SCRATCH / f"{args.tag}bestof_agentdojo_{sname}"
        for tid in sorted(adj.user_tasks):
            ut = adj.user_tasks[tid]
            td = base / tid
            cands = [f.read_text() for f in sorted(td.glob("cand*.py"))] if td.exists() else []
            try:
                gt = [(fc.function, _positional(fc, params)) for fc in ut.ground_truth(suite.make_env())]
            except Exception:  # noqa: BLE001
                continue
            entries = [_exec(suite, c) for c in cands]
            valid = [e for e in entries if e[0] is not None]
            totals["tasks"] += 1
            if not valid:
                rows.append({"suite": sname, "task": tid, "status": "no_plan",
                             "gt": [t for t, _ in gt], "reasons": [e[3] for e in entries]})
                totals["no_plan"] += 1
                for t, _ in gt:
                    miss_by_tool[t] += 1
                    miss_kind[("no_plan", _shape(t, docs))] += 1
                continue
            sel = max(valid, key=lambda e: (e[1], sum(1 for t, _ in e[2] if is_side_effecting(t))))
            observed = [(t, a) for t, a in sel[2] if t not in GATEWAY_INTERNAL_TOOLS]
            excess, missing = _match_lists(gt, observed, docs)
            # Is the missing tool present in the plan at all (wrong operand) or absent?
            plan_tools = {t for t, _ in sel[2]}
            miss_detail = []
            for t, a in missing:
                kind = "wrong_operand" if t in plan_tools else "absent"
                miss_detail.append({"tool": t, "kind": kind, "shape": _shape(t, docs), "gt_args": [str(x)[:60] for x in a]})
                miss_by_tool[t] += 1
                miss_kind[(kind, _shape(t, docs))] += 1
            exc_detail = []
            gt_tools = {t for t, _ in gt}
            for t, a in excess:
                kind = "extra_call_same_tool" if t in gt_tools else "unrequested_tool"
                exc_detail.append({"tool": t, "kind": kind, "shape": _shape(t, docs), "args": [str(x)[:60] for x in a]})
                exc_by_tool[t] += 1
                exc_kind[(kind, _shape(t, docs))] += 1
            status = "exact" if not excess and not missing else ("missing" if missing and not excess else ("excess" if excess and not missing else "both"))
            totals[status] += 1
            rows.append({"suite": sname, "task": tid, "status": status, "clean": sel[1],
                         "prompt": ut.PROMPT, "code": sel[0].source if hasattr(sel[0], "source") else "",
                         "gt": [(t, [str(x)[:40] for x in a]) for t, a in gt],
                         "trace": [(t, [str(x)[:40] for x in a]) for t, a in observed],
                         "missing": miss_detail, "excess": exc_detail})

    print(f"tasks={totals['tasks']} exact={totals['exact']} missing_only={totals['missing']} "
          f"excess_only={totals['excess']} both={totals['both']} no_plan={totals['no_plan']}")
    print("\nMISSING by (kind, shape):", dict(miss_kind))
    print("MISSING by tool:", miss_by_tool.most_common(15))
    print("\nEXCESS by (kind, shape):", dict(exc_kind))
    print("EXCESS by tool:", exc_by_tool.most_common(15))
    if args.verbose:
        for r in rows:
            if r["status"] in ("missing", "excess", "both", "no_plan"):
                print(f"\n== {r['suite']}/{r['task']} [{r['status']}]")
                print("  prompt:", r.get("prompt", "")[:200].replace("\n", " "))
                for m in r.get("missing", []):
                    print("  MISSING", m)
                for e in r.get("excess", []):
                    print("  EXCESS ", e)
    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=1))
        print("wrote", args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
