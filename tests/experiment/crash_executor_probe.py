"""Measure the crash-repair executor on exactly the tasks that currently crash.

The four AgentDojo tasks whose every cached candidate crashes at runtime are
regenerated WITH the crash-probe executor wired into the
Planner's self-repair loop -- it dry-runs each candidate against a mock env and
feeds the crash back for repair. Reports before (all crash) -> after (does the
selected candidate now run clean?). Targeted, so it costs ~4 tasks of API, not 97.

Loads OPENAI_API_KEY from .env. Usage: python -m tests.experiment.crash_executor_probe
"""
from __future__ import annotations

import os
from pathlib import Path

# load .env (OPENAI_API_KEY) -- the Planner generator needs it
for line in (Path(".env").read_text().splitlines() if Path(".env").exists() else []):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))

from agentdojo.task_suite.load_suites import get_suites  # noqa: E402
from benchmarks.agentdojo_adapter import load_suite  # noqa: E402
from benchmarks.structured_read import augment_with_structuring  # noqa: E402
from gateway.planning.agentic_planner import generate_code_with_self_repair  # noqa: E402
from gateway.runtime.confirmation import is_side_effecting  # noqa: E402
from pauth import prepare  # noqa: E402
from pauth.enforcer import Enforcer  # noqa: E402
from pauth.tool_executor import execute_generated_code  # noqa: E402
from pauth.envelope import EnvelopeStore, KeyRing  # noqa: E402
from pauth.grammar_validator import DSLRejectionError  # noqa: E402

CRASHERS = [("travel", "user_task_4"), ("travel", "user_task_7"),
            ("travel", "user_task_8"), ("workspace", "user_task_33")]
N = 3
OUT = Path("tests/experiment/funnel_scratch")


def _probe(suite):
    def probe(code):
        try:
            prep = prepare(code, suite.tool_names(), suite.tool_signer())
        except DSLRejectionError:
            return None
        enf = Enforcer(prep.rules, EnvelopeStore(KeyRing()), suite.tool_signer())
        rep = execute_generated_code(prep.source, enf, suite.tool_params(),
                                     suite.tool_executor_factory(suite.make_env()))
        return rep.crashed
    return probe


def _runs_clean(suite, code):
    try:
        prep = prepare(code, suite.tool_names(), suite.tool_signer())
    except DSLRejectionError:
        return False, "grammar"
    enf = Enforcer(prep.rules, EnvelopeStore(KeyRing()), suite.tool_signer())
    rep = execute_generated_code(prep.source, enf, suite.tool_params(),
                                 suite.tool_executor_factory(suite.make_env()))
    if rep.crashed:
        return False, rep.crashed[:60]
    if rep.denied:
        return False, "denied"
    return True, "clean"


def main():
    recovered = 0
    for sname, tid in CRASHERS:
        adj = get_suites("v1")[sname]
        suite = augment_with_structuring(load_suite(sname))
        prompt = adj.user_tasks[tid].PROMPT
        d = OUT / f"struct_exec_gpt-5_1_bestof_agentdojo_{sname}" / tid
        d.mkdir(parents=True, exist_ok=True)
        best_clean = False
        for i in range(N):
            pf = d / f"cand{i}.py"
            if pf.exists():
                code = pf.read_text()
            else:
                res = generate_code_with_self_repair(
                    prompt + ("" if i == 0 else f"\n(variant {i})"),
                    suite.tool_docs(), model="gpt-5.1", max_retries=3,
                    enable_judge=False, executor=_probe(suite))
                code = res.code
                pf.write_text(code)
            clean, why = _runs_clean(suite, code)
            if clean:
                best_clean = True
        recovered += best_clean
        print(f"  {sname}/{tid}: before=CRASH  after={'CLEAN (recovered)' if best_clean else 'still crash'}")
    print(f"\ncrash-repair executor: recovered {recovered}/{len(CRASHERS)} runtime crashes "
          f"(crash-free compiled plans 88 -> {88 + recovered}/92; reference fidelity "
          f"is unchanged unless the recovered run also fills its calls)")


if __name__ == "__main__":
    main()
