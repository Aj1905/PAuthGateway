"""E3: best-of-N sampling (search, not prompt-tuning). Generate N candidates per
task; SELECT the first that is DSL-valid AND runs clean (no crash/deny) -- a
deployment-available criterion, no ground truth. Measures whether a search-based
lift generalizes across frameworks where prompt-tuning did not.

banking: G2/G5/GS ; tau_retail: G2/FID/GS. Candidates cached to scratch.
Needs OPENAI_API_KEY. Usage: python -m tests.experiment.bestofn_experiment [N]
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

from agentdojo.task_suite.load_suites import get_suites

from benchmarks.agentdojo_adapter import load_suite
from benchmarks.forced_injection import generate_for_task
from benchmarks.tau_bench_adapter import build_suite as build_tau
from gateway.planning.agentic_planner import generate_code_with_self_repair
from pauth import prepare
from pauth.enforcer import Enforcer, check_injection
from pauth.tool_executor import execute_generated_code
from pauth.envelope import EnvelopeStore, KeyRing
from pauth.grammar_validator import DSLRejectionError

SCRATCH = Path("tests/experiment/bestofn_scratch")
N = int(sys.argv[1]) if len(sys.argv) > 1 else 3


def _runs_clean(suite, code) -> bool:
    try:
        prepared = prepare(code, suite.tool_names(), suite.tool_signer())
    except DSLRejectionError:
        return False
    enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), suite.tool_signer())
    rep = execute_generated_code(prepared.source, enf, suite.tool_params(),
                                 suite.tool_executor_factory(suite.make_env()))
    return rep.crashed is None and not rep.denied


def _select(suite, cands: list[str]) -> str:
    valid = []
    for c in cands:
        try:
            prepare(c, suite.tool_names(), suite.tool_signer())
        except DSLRejectionError:
            continue
        valid.append(c)
        if _runs_clean(suite, c):
            return c            # first DSL-valid that runs clean
    return valid[0] if valid else cands[0]


def _candidates(suite, prompt, key) -> list[str]:
    d = SCRATCH / key
    d.mkdir(parents=True, exist_ok=True)
    cands = []
    for i in range(N):
        pf = d / f"cand{i}.py"
        if pf.exists():
            cands.append(pf.read_text()); continue
        res = generate_code_with_self_repair(
            prompt + ("" if i == 0 else f"\n(attempt {i})"),
            suite.tool_docs(), model="gpt-4.1", max_retries=3, enable_judge=False)
        pf.write_text(res.code)
        cands.append(res.code)
    return cands


def _g2gs(suite, code, injections):
    try:
        prepared = prepare(code, suite.tool_names(), suite.tool_signer())
    except DSLRejectionError:
        return False, True
    enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), suite.tool_signer())
    execute_generated_code(prepared.source, enf, suite.tool_params(),
                           suite.tool_executor_factory(suite.make_env()))
    gs = all(not check_injection(enf, c.tool, list(c.args)).permit for c in injections)
    return True, gs


def run_banking(limit=16):
    adj = get_suites("v1")["banking"]; suite = load_suite("banking")
    g2 = g5 = gs = 0; n = 0
    for tid in sorted(adj.user_tasks)[:limit]:
        ut = adj.user_tasks[tid]; n += 1
        cands = _candidates(suite, ut.PROMPT, f"banking_{tid}")
        code = _select(suite, cands)
        ok2, ok_s = _g2gs(suite, code, [])
        # G5
        env = suite.make_env(); pre = copy.deepcopy(env)
        prepared = prepare(code, suite.tool_names(), suite.tool_signer()) if ok2 else None
        ok5 = False
        if prepared:
            enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), suite.tool_signer())
            rep = execute_generated_code(prepared.source, enf, suite.tool_params(),
                                         suite.tool_executor_factory(env))
            if rep.crashed is None and not rep.denied:
                try: ok5 = bool(ut.utility("", pre, env))
                except Exception: ok5 = False
            enf2 = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), suite.tool_signer())
            execute_generated_code(prepared.source, enf2, suite.tool_params(), suite.tool_executor_factory(suite.make_env()))
            ok_s = all(not check_injection(enf2, c.tool, list(c.args)).permit
                       for c in generate_for_task(adj, ut, suite.tool_params(), suite.make_env))
        g2 += ok2; g5 += ok5; gs += ok_s
    print(f"banking best-of-{N}: G2={g2}/{n} G5={g5}/{n} GS={gs}/{n}")


def run_tau(limit=15):
    suite = build_tau()
    ref = {t.id: t for t in suite.tasks}
    g2 = gs = 0; n = 0
    for t in suite.tasks[:limit]:
        n += 1
        cands = _candidates(suite, t.prompt, f"tau_{t.id}")
        code = _select(suite, cands)
        ok2, ok_s = _g2gs(suite, code, t.forced_injections)
        g2 += ok2; gs += ok_s
    print(f"tau best-of-{N}: G2={g2}/{n} GS={gs}/{n} (FID omitted, ~0 at baseline)")


if __name__ == "__main__":
    run_banking()
    run_tau()
