"""Reference-fidelity tradeoff with planner candidates fixed and selection varied.

Re-selects from the SAME cached gpt-5.1 best-of-N candidates under two policies:
  MAX  -- clean, then MOST side-effecting calls
  MIN  -- clean, then FEWEST side-effecting >0
and measures lifecycle diagnostics, both reference-fidelity halves, their exact
conjunction, outcome, and cost. No API calls are made because candidates are
cached, so this isolates selection policy from Planner generation.

Usage: python -m tests.experiment.selection_tradeoff
"""
from __future__ import annotations

import copy
from pathlib import Path

from agentdojo.task_suite.load_suites import get_suites

from benchmarks.agentdojo_adapter import load_suite
from benchmarks.structured_read import augment_with_structuring
from eval.gates import _fidelity_control, _permissive_runtime_crash
from eval.metrics import (
    CONFORMANCE_PLAN_TRACE_PERMITTED,
    COST_TOOL_CALLS,
    OUTCOME_TASK_COMPLETED,
    GT_EXACT_AUTHORIZATION,
    GT_NO_EXCESS_CALLS,
    GT_NO_MISSING_CALLS,
    RELIABILITY_RUNTIME_CRASH_FREE,
    SYNTHESIS_POLICY_COMPILED,
)
from gateway.runtime.confirmation import is_side_effecting
from pauth import prepare
from pauth.enforcer import Enforcer
from pauth.tool_executor import execute_generated_code
from pauth.envelope import EnvelopeStore, KeyRing
from pauth.grammar_validator import DSLRejectionError

SCRATCH = Path("tests/experiment/funnel_scratch")
SUITES = ["banking", "slack", "travel", "workspace"]
TOTAL = 97  # full agentdojo user-task count -> percentages are /97


def _exec(suite, code):
    """Return prepared plan, enforced report, and permitted trace."""
    try:
        prepared = prepare(code, suite.tool_names(), suite.tool_signer())
    except DSLRejectionError:
        return None, None, []
    enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), suite.tool_signer())
    rep = execute_generated_code(prepared.source, enf, suite.tool_params(),
                                 suite.tool_executor_factory(suite.make_env()))
    trace = [(e.tool, list(e.args)) for e in rep.events if e.decision.permit]
    return prepared, rep, trace


def _nse(trace):
    return sum(1 for t, _ in trace if is_side_effecting(t))


def _select(suite, cands, policy):
    """policy='max' -> clean then most side-effecting; 'min' -> clean-and-acts then
    fewest side-effecting. Fall back to any clean, then first valid, then first."""
    scored = []
    for c in cands:
        prepared, rep, trace = _exec(suite, c)
        if prepared is None:
            continue
        clean = rep.crashed is None and not rep.denied
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
    measured = (
        SYNTHESIS_POLICY_COMPILED,
        RELIABILITY_RUNTIME_CRASH_FREE,
        CONFORMANCE_PLAN_TRACE_PERMITTED,
        GT_NO_MISSING_CALLS,
        GT_NO_EXCESS_CALLS,
        GT_EXACT_AUTHORIZATION,
        OUTCOME_TASK_COMPLETED,
    )
    agg = {k: 0 for k in measured}
    cost_calls = cost_n = considered = 0
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

            prepared, rep, trace = _exec(suite, code)
            if prepared is None:
                excess, missing = _fidelity_control(
                    ut, suite, suite.tool_params(), [], docs
                )
                agg[GT_NO_MISSING_CALLS] += (missing == 0)
                agg[GT_NO_EXCESS_CALLS] += (excess == 0)
                agg[GT_EXACT_AUTHORIZATION] += (excess == 0 and missing == 0)
                continue
            agg[SYNTHESIS_POLICY_COMPILED] += 1
            agg[RELIABILITY_RUNTIME_CRASH_FREE] += (
                _permissive_runtime_crash(suite, prepared.source) is None
            )
            agg[CONFORMANCE_PLAN_TRACE_PERMITTED] += (not rep.denied)
            cost_calls += len(trace)
            cost_n += 1

            # OUTCOME via utility (fresh env)
            env = suite.make_env(); pre = copy.deepcopy(env)
            enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), suite.tool_signer())
            execute_generated_code(
                prepared.source, enf, suite.tool_params(), suite.tool_executor_factory(env)
            )
            try:
                agg[OUTCOME_TASK_COMPLETED] += bool(ut.utility("", pre, env))
            except Exception:  # noqa: BLE001
                pass

            excess, missing = _fidelity_control(
                ut, suite, suite.tool_params(), trace, docs
            )
            agg[GT_NO_MISSING_CALLS] += (missing == 0)
            agg[GT_NO_EXCESS_CALLS] += (excess == 0)
            agg[GT_EXACT_AUTHORIZATION] += (excess == 0 and missing == 0)
    agg[COST_TOOL_CALLS] = cost_calls / max(1, cost_n)
    return agg, considered


def main():
    print(f"gpt-5.1 struct best-of-N, planner FIXED, selection VARIED (n=/{TOTAL})\n")
    rows = {}
    for pol in ("max", "min"):
        rows[pol], considered = measure(pol)
    hdr = f"{'metric':22} {'MAX(most-acting)':>18} {'MIN(minimal)':>14}"
    print(hdr); print("-" * len(hdr))
    labels = [
        SYNTHESIS_POLICY_COMPILED,
        RELIABILITY_RUNTIME_CRASH_FREE,
        CONFORMANCE_PLAN_TRACE_PERMITTED,
        GT_NO_MISSING_CALLS,
        GT_NO_EXCESS_CALLS,
        GT_EXACT_AUTHORIZATION,
        OUTCOME_TASK_COMPLETED,
    ]
    for name in labels:
        k = name
        mx, mn = rows["max"][k], rows["min"][k]
        mx_den = (
            rows["max"][SYNTHESIS_POLICY_COMPILED]
            if k in {
                RELIABILITY_RUNTIME_CRASH_FREE,
                CONFORMANCE_PLAN_TRACE_PERMITTED,
            }
            else TOTAL
        )
        mn_den = (
            rows["min"][SYNTHESIS_POLICY_COMPILED]
            if k in {
                RELIABILITY_RUNTIME_CRASH_FREE,
                CONFORMANCE_PLAN_TRACE_PERMITTED,
            }
            else TOTAL
        )
        print(
            f"{name:22} {mx:>4}/{mx_den} ({100 * mx // max(1, mx_den):>2}%)"
            f"   {mn:>4}/{mn_den} ({100 * mn // max(1, mn_den):>2}%)"
        )
    print(f"{COST_TOOL_CALLS:22} {rows['max'][COST_TOOL_CALLS]:>17.2f} "
          f"{rows['min'][COST_TOOL_CALLS]:>14.2f}")
    print(f"\nconsidered tasks (cached candidates present): {considered}/{TOTAL}")


if __name__ == "__main__":
    main()
