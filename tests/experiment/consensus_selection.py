"""GT-free selection policies over cached gpt-5.1 struct best-of candidates.

T15 (GT_NO_MISSING_IMPROVEMENT_LOG.md): measured after the T14 measurement
correction and NOT adopted -- exact +3/97 (32->35, not significant under a
paired sign test) at the cost of OUTCOME -1, designed and evaluated on the
same cached candidate set. The funnel's default selection stays MAX.

Policies (all deployment-legal: no ground truth consulted):
  max       -- clean, then most side-effecting (current)
  consensus -- clean, then maximize sum of cross-candidate support of the
               candidate's side-effecting calls minus penalty for minority
               calls, then most side-effecting
  majority  -- clean, then minimize |candidate's SE call set XOR majority set|
Reports fidelity + outcome per policy. No API calls (candidates are cached).

Usage: python -m tests.experiment.consensus_selection
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import copy

from agentdojo.task_suite.load_suites import get_suites

from benchmarks.agentdojo_adapter import load_suite
from benchmarks.structured_read import augment_with_structuring
from eval.gates import _fidelity_control
from gateway.planning.prechecks import PrecheckPolicy
from gateway.runtime.confirmation import control_operands, is_side_effecting
from pauth import prepare
from pauth.enforcer import Enforcer
from pauth.tool_executor import execute_generated_code
from pauth.envelope import EnvelopeStore, KeyRing
from pauth.grammar_validator import DSLRejectionError

SCRATCH = Path("tests/experiment/funnel_scratch")
SUITES = ["banking", "slack", "travel", "workspace"]


def _exec(suite, code):
    try:
        prepared = prepare(code, suite.tool_names(), suite.tool_signer())
    except DSLRejectionError:
        return None, False, []
    enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), suite.tool_signer())
    rep = execute_generated_code(prepared.source, enf, suite.tool_params(),
                                 suite.tool_executor_factory(suite.make_env()))
    trace = [(e.tool, list(e.args)) for e in rep.events if e.decision.permit]
    return prepared, (rep.crashed is None and not rep.denied), trace


def _se_key_multiset(trace, docs, pol):
    keys = []
    for tool, args in trace:
        if not is_side_effecting(tool):
            continue
        idx = [i for i, _ in control_operands(tool, docs, pol)]
        keys.append((tool, tuple(str(args[i]) if i < len(args) else None for i in idx)))
    return Counter(keys)


def select(policy, entries, docs, pol):
    # entries: list of (code, clean, trace)
    valid = [e for e in entries if e[0] is not None]
    if not valid:
        return None
    if policy == "max":
        return max(valid, key=lambda e: (e[1], sum(1 for t, _ in e[2] if is_side_effecting(t))))
    msets = {id(e): _se_key_multiset(e[2], docs, pol) for e in valid}
    support = Counter()
    for e in valid:
        support.update(set(msets[id(e)]))
    n = len(valid)
    if policy == "consensus":
        def score(e):
            ms = msets[id(e)]
            sup = sum(support[k] for k in ms)          # cross-candidate agreement
            minority = sum(1 for k in ms if support[k] <= n // 2)
            return (e[1], -minority, sup, sum(ms.values()))
        return max(valid, key=score)
    if policy == "majority":
        maj = {k for k, c in support.items() if c > n / 2}
        def score(e):
            ms = set(msets[id(e)])
            return (e[1], -len(ms ^ maj), sum(msets[id(e)].values()))
        return max(valid, key=score)
    raise ValueError(policy)


def main():
    for policy in ("max", "consensus", "majority"):
        agg = Counter()
        for sname in SUITES:
            adj = get_suites("v1")[sname]
            suite = augment_with_structuring(load_suite(sname))
            docs = {n: s.doc for n, s in suite.tools.items()}
            pol = PrecheckPolicy()
            base = SCRATCH / f"struct_gpt-5_1_bestof_agentdojo_{sname}"
            for tid in sorted(adj.user_tasks):
                td = base / tid
                cands = [f.read_text() for f in sorted(td.glob("cand*.py"))] if td.exists() else []
                if not cands:
                    continue
                ut = adj.user_tasks[tid]
                entries = [_exec(suite, c) for c in cands]
                sel = select(policy, entries, docs, pol)
                trace = sel[2] if sel else []
                exc, mis = _fidelity_control(ut, suite, suite.tool_params(), trace, docs)
                if exc is None:
                    continue
                agg["nomiss"] += (mis == 0)
                agg["noexc"] += (exc == 0)
                agg["exact"] += (mis == 0 and exc == 0)
                # outcome
                if sel and sel[0] is not None:
                    env = suite.make_env(); pre = copy.deepcopy(env)
                    enf = Enforcer(sel[0].rules, EnvelopeStore(KeyRing()), suite.tool_signer())
                    execute_generated_code(sel[0].source, enf, suite.tool_params(),
                                           suite.tool_executor_factory(env))
                    try:
                        agg["outcome"] += bool(ut.utility("", pre, env))
                    except Exception:  # noqa: BLE001
                        pass
        print(f"{policy:10} miss0={agg['nomiss']}/97 exc0={agg['noexc']}/97 "
              f"exact={agg['exact']}/97 outcome={agg['outcome']}/97")


if __name__ == "__main__":
    main()
