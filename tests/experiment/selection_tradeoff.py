"""Availability vs least-authority tradeoff, planner FIXED, selection VARIED.

Re-selects from the SAME cached gpt-5.1 best-of-N candidates under two policies:
  MAX  -- clean, then MOST side-effecting calls   (current: maximizes AVAIL_4)
  MIN  -- clean, then FEWEST side-effecting >0     (minimal-complete: minimizes excess)
and measures AVAIL_2/3/4, OUTCOME, SEC_NO_EXCESS, COST for each. No API calls
(candidates are cached), so this isolates the selection policy from the planner.

Usage: python -m tests.experiment.selection_tradeoff
"""
from __future__ import annotations

import copy
from pathlib import Path

from agentdojo.task_suite.load_suites import get_suites

from benchmarks.agentdojo_adapter import load_suite
from benchmarks.structured_read import augment_with_structuring
from eval.gates import _deficiency_control, _excess_deficiency, _positional
from gateway.runtime.confirmation import is_side_effecting
from pauth import prepare
from pauth.enforcer import Enforcer, execute_generated_code
from pauth.envelope import EnvelopeStore, KeyRing
from pauth.grammar import RestrictedGrammarError

SCRATCH = Path("tests/experiment/funnel_scratch")
SUITES = ["banking", "slack", "travel", "workspace"]
TOTAL = 97  # full agentdojo user-task count -> percentages are /97


def _exec(suite, code):
    """Return (prepared_ok, ran_clean, trace) for one plan; trace = permitted calls."""
    try:
        prepared = prepare(code, suite.tool_names(), suite.tool_signer())
    except RestrictedGrammarError:
        return None, False, []
    enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), suite.tool_signer())
    rep = execute_generated_code(prepared.source, enf, suite.tool_params(),
                                 suite.runner_factory(suite.make_env()))
    clean = rep.crashed is None and not rep.denied
    trace = [(e.tool, list(e.args)) for e in rep.events if e.decision.permit]
    return prepared, clean, trace


def _nse(trace):
    return sum(1 for t, _ in trace if is_side_effecting(t))


def _select(suite, cands, policy):
    """policy='max' -> clean then most side-effecting; 'min' -> clean-and-acts then
    fewest side-effecting. Fall back to any clean, then first valid, then first."""
    scored = []
    for c in cands:
        prepared, clean, trace = _exec(suite, c)
        if prepared is None:
            continue
        scored.append((c, clean, _nse(trace)))
    if not scored:
        return cands[0]
    if policy == "max":
        return max(scored, key=lambda s: (1 if s[1] else 0, s[2]))[0]
    # min: prefer clean-and-acting with the fewest side-effecting calls
    acting = [s for s in scored if s[1] and s[2] > 0]
    if acting:
        return min(acting, key=lambda s: s[2])[0]
    clean = [s for s in scored if s[1]]
    return (clean or scored)[0][0]


def measure(policy):
    agg = {k: 0 for k in ("A2", "A3", "A4", "OUT", "NOEX", "COST")}
    cost_calls = considered = 0
    for sname in SUITES:
        adj = get_suites("v1")[sname]
        suite = augment_with_structuring(load_suite(sname))
        docs = {n: s.doc for n, s in suite.tools.items()}
        base = SCRATCH / f"struct_gpt-5_1_bestof_agentdojo_{sname}"
        for tid in sorted(adj.user_tasks):
            td = base / tid
            if not td.exists():
                continue
            cands = [f.read_text() for f in sorted(td.glob("cand*.py"))]
            if not cands:
                continue
            considered += 1
            ut = adj.user_tasks[tid]
            code = _select(suite, cands, policy)

            prepared, clean, trace = _exec(suite, code)
            if prepared is None:
                continue
            agg["A2"] += 1
            # OUTCOME via utility (fresh env)
            env = suite.make_env(); pre = copy.deepcopy(env)
            enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), suite.tool_signer())
            rep = execute_generated_code(prepared.source, enf, suite.tool_params(),
                                         suite.runner_factory(env))
            if rep.crashed is None and not rep.denied:
                agg["A3"] += 1
                cost_calls += sum(1 for e in rep.events if e.decision.permit)
                try:
                    if bool(ut.utility("", pre, env)):
                        agg["OUT"] += 1
                except Exception:  # noqa: BLE001
                    pass
            # AVAIL_4 (deficiency on control operands == 0)
            defc = _deficiency_control(ut, suite, suite.tool_params(), trace, docs)
            if defc == 0:
                agg["A4"] += 1
            # SEC_NO_EXCESS (excess vs GT == 0)
            excess, _ = _excess_deficiency(ut, suite, suite.tool_params(), trace)
            if excess == 0:
                agg["NOEX"] += 1
    agg["COST"] = cost_calls / max(1, agg["A3"])
    return agg, considered


def main():
    print(f"gpt-5.1 struct best-of-N, planner FIXED, selection VARIED (n=/{TOTAL})\n")
    rows = {}
    for pol in ("max", "min"):
        rows[pol], considered = measure(pol)
    hdr = f"{'metric':22} {'MAX(most-acting)':>18} {'MIN(minimal)':>14}"
    print(hdr); print("-" * len(hdr))
    labels = [("A2", "AVAIL_2_PLAN_VALID"), ("A3", "AVAIL_3_RAN_CLEAN"),
              ("A4", "AVAIL_4_CALLS_MADE"), ("OUT", "OUTCOME_COMPLETED"),
              ("NOEX", "SEC_NO_EXCESS")]
    for k, name in labels:
        mx, mn = rows["max"][k], rows["min"][k]
        print(f"{name:22} {mx:>4}/{TOTAL} ({100*mx//TOTAL:>2}%)   {mn:>4}/{TOTAL} ({100*mn//TOTAL:>2}%)")
    print(f"{'COST_TOOL_CALLS':22} {rows['max']['COST']:>17.2f} {rows['min']['COST']:>14.2f}")
    print(f"\nconsidered tasks (cached candidates present): {considered}/{TOTAL}")


if __name__ == "__main__":
    main()
