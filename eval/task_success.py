"""Ground-truth TASK SUCCESS: did the executed plan actually accomplish the task?

The other evals stop at "grammar-accepted" (a plan was produced) and "clean"
(it ran without crashing or a false denial). Neither tells you whether the plan
DID THE RIGHT THING. AgentDojo ships that ground truth: every user task has a
deterministic ``utility(model_output, pre_env, post_env)`` that inspects the
post-execution environment and returns True iff the goal state was reached.

For each task we take the plan (cached A1), execute it through the real enforcer
against a fresh environment, snapshot the environment before/after, and call the
task's own utility. No LLM judge, no API, no noise -- success is the task's own
definition.

Three levels are reported per suite and overall:
  1. grammar-accepted -- A1 produced a plan that passed grammar/slice/rule.
  2. clean            -- that plan ran without a runtime crash or false denial.
  3. TASK SUCCESS     -- utility() confirms the goal state was reached.

Caveat: PAuth executes tool calls; it does not emit a chat answer. Tasks whose
utility checks ``model_output`` (pure "tell me X" retrieval) are scored against
an empty output and will read as failures even when the plan read the right
data. That undercounts success on information-retrieval tasks -- a framing
artifact of measuring an action executor, flagged in the output.

Run:  .venv/bin/python -m eval.task_success
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
from pathlib import Path

from agentdojo.task_suite.load_suites import get_suites

from benchmarks.agentdojo_adapter import load_suite
from pauth import prepare
from pauth.enforcer import Enforcer, execute_generated_code
from pauth.envelope import EnvelopeStore, KeyRing
from pauth.grammar import RestrictedGrammarError

from eval.fpfn import _runtime_probe, load_env_file

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "tests" / "experiment" / "cache"
_SUITES = ("banking", "slack", "travel", "workspace")


@dataclasses.dataclass
class Outcome:
    task_id: str
    level: str            # "skip" | "crash" | "fail" | "success"
    detail: str = ""


def _plan_cached(suite_name: str, task_id: str) -> str | None:
    p = CACHE_DIR / suite_name / f"{task_id}.py"
    return p.read_text() if p.exists() else None


def _plan_agentic(suite_name: str, task_id: str, prompt: str, spec,
                  model: str, judge_model: str, max_retries: int) -> str | None:
    """Generate a plan through the agentic self-repair pipeline (grammar repair +
    runtime-crash repair + intent judge), the path whose task-success we test."""
    from gateway.planning.agentic_a1 import generate_code_with_self_repair
    cache_path = (CACHE_DIR / suite_name / "agentic-ts"
                  / f"{task_id}-{model}-r{max_retries}-j-{judge_model}.py")
    try:
        res = generate_code_with_self_repair(
            prompt, spec.tool_docs(), model=model, max_retries=max_retries,
            cache_path=cache_path, enable_judge=True, judge_model=judge_model,
            executor=_runtime_probe(spec),
        )
    except Exception as exc:  # noqa: BLE001 -- API/key error: treat as no plan
        return f"# gen-error: {type(exc).__name__}: {exc}"
    return res.code


def measure_suite(suite_name: str, planner: str = "cached", model: str = "gpt-4.1",
                  judge_model: str = "gpt-4.1", max_retries: int = 3) -> list[Outcome]:
    adj = get_suites("v1")[suite_name]
    spec = load_suite(suite_name)
    tools, signer, params = spec.tool_names(), spec.tool_signer(), spec.tool_params()
    out: list[Outcome] = []
    for task_id in sorted(adj.user_tasks):
        ut = adj.user_tasks[task_id]
        if planner == "agentic":
            code = _plan_agentic(suite_name, task_id, ut.PROMPT, spec,
                                 model, judge_model, max_retries)
        else:
            code = _plan_cached(suite_name, task_id)
        if code is None:
            out.append(Outcome(task_id, "skip", "no cached plan"))
            continue
        try:
            prepared = prepare(code, tools, signer)
        except RestrictedGrammarError as exc:
            out.append(Outcome(task_id, "skip", f"grammar: {exc}"))
            continue
        env = spec.make_env()
        pre = copy.deepcopy(env)
        enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), signer)
        rep = execute_generated_code(prepared.source, enf, params, spec.runner_factory(env))
        if rep.crashed:
            out.append(Outcome(task_id, "crash", rep.crashed))
            continue
        if rep.denied:
            out.append(Outcome(task_id, "fail", "false denial"))
            continue
        try:
            ok = bool(ut.utility("", pre, env))
        except Exception as exc:  # noqa: BLE001 -- utility can raise on odd envs
            out.append(Outcome(task_id, "fail", f"utility error: {type(exc).__name__}"))
            continue
        out.append(Outcome(task_id, "success" if ok else "fail",
                           "" if ok else "utility=False (goal state not reached)"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Ground-truth task-success measurement.")
    ap.add_argument("--planner", choices=["cached", "agentic"], default="cached",
                    help="cached one-shot A1, or the agentic self-repair pipeline")
    ap.add_argument("--model", default="gpt-4.1")
    ap.add_argument("--judge-model", default="gpt-4.1", help="intent judge (OpenAI-family reuses the key)")
    ap.add_argument("--max-retries", type=int, default=3)
    args = ap.parse_args()
    load_env_file()

    print(f"Ground-truth task success (AgentDojo utility) -- {args.planner} plans\n")
    hdr = f"{'suite':<12}{'tasks':>6}{'accepted':>10}{'clean':>8}{'SUCCESS':>9}{'rate':>8}"
    print(hdr); print("-" * len(hdr))
    T = A = C = S = 0
    for name in _SUITES:
        res = measure_suite(name, planner=args.planner, model=args.model,
                            judge_model=args.judge_model, max_retries=args.max_retries)
        n = len(res)
        accepted = sum(1 for r in res if r.level in ("crash", "fail", "success"))
        clean = sum(1 for r in res if r.level in ("fail", "success"))
        success = sum(1 for r in res if r.level == "success")
        T += n; A += accepted; C += clean; S += success
        rate = f"{success / n:.0%}" if n else "-"
        print(f"{name:<12}{n:>6}{accepted:>10}{clean:>8}{success:>9}{rate:>8}")
    print("-" * len(hdr))
    print(f"{'OVERALL':<12}{T:>6}{A:>10}{C:>8}{S:>9}{S / T:>7.1%}")
    print(f"\n  grammar-accepted {A}/{T} = {A/T:.1%}   ->   clean {C}/{T} = {C/T:.1%}"
          f"   ->   TASK SUCCESS {S}/{T} = {S/T:.1%}")
    print("\n  TASK SUCCESS is the task's own ground-truth utility() on the post-execution")
    print("  environment. Note: retrieval tasks whose utility checks the chat answer are")
    print("  scored against an empty output (PAuth executes actions, emits no text), so")
    print("  this UNDERcounts success on 'tell me X' tasks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
