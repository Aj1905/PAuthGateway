"""Decompose the 14% headless ceiling: among grammar-valid (G2-pass) cached
plans that FAIL G5, how many are

  HOLLOW  -- ground truth needs a side-effecting call but the plan issues NONE
             (the Planner dropped an un-expressible step; a grammar wall,
             regeneration cannot cross);
  WRONG   -- the plan DOES issue side-effecting calls but still fails G5
             (wrong args / excess / deficiency; a correct grammar-valid plan may
             exist, so REGENERATION could plausibly fix it);
  GT_READ -- ground truth has no side-effecting call (a pure query task).

The WRONG count is the upper bound on what LLM slice-regeneration could recover
on this bench without any grammar change.
"""

from __future__ import annotations

import copy

from agentdojo.task_suite.load_suites import get_suites

from benchmarks.agentdojo_adapter import load_suite
from eval.gates import _positional
from gateway.runtime.confirmation import is_side_effecting
from pauth import prepare
from pauth.enforcer import Enforcer, execute_generated_code
from pauth.envelope import EnvelopeStore, KeyRing
from pauth.grammar import RestrictedGrammarError
from pathlib import Path

CACHE = Path("tests/experiment/cache")
SUITES = ["banking", "slack", "travel", "workspace"]


def main() -> None:
    tally = {"HOLLOW": [], "WRONG": [], "GT_READ": []}
    g2 = g5 = 0
    for s in SUITES:
        adj = get_suites("v1")[s]
        spec = load_suite(s)
        tools, signer, params = spec.tool_names(), spec.tool_signer(), spec.tool_params()
        for tid in sorted(adj.user_tasks):
            ut = adj.user_tasks[tid]
            p = CACHE / s / f"{tid}.py"
            if not p.exists():
                continue
            try:
                prepared = prepare(p.read_text(), tools, signer)
            except RestrictedGrammarError:
                continue
            g2 += 1
            env = spec.make_env()
            pre = copy.deepcopy(env)
            enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), signer)
            rep = execute_generated_code(prepared.source, enf, params, spec.runner_factory(env))
            ok = rep.crashed is None and not rep.denied
            g5_pass = False
            if ok:
                try:
                    g5_pass = bool(ut.utility("", pre, env))
                except Exception:  # noqa: BLE001
                    g5_pass = False
            if g5_pass:
                g5 += 1
                continue
            # failing plan: classify
            plan_se = [e.tool for e in rep.events if e.decision.permit and is_side_effecting(e.tool)]
            try:
                gt = ut.ground_truth(spec.make_env())
            except Exception:  # noqa: BLE001
                gt = []
            gt_se = [fc.function for fc in gt if is_side_effecting(fc.function)]
            if not gt_se:
                tally["GT_READ"].append(f"{s}/{tid}")
            elif not plan_se:
                tally["HOLLOW"].append(f"{s}/{tid}")
            else:
                tally["WRONG"].append(f"{s}/{tid}")

    print(f"grammar-valid (G2) plans: {g2}")
    print(f"reached G5 (success):     {g5}")
    print(f"failed G5:                {g2 - g5}\n")
    for k in ("HOLLOW", "WRONG", "GT_READ"):
        v = tally[k]
        print(f"{k:8} {len(v):3}  {', '.join(v)}")
    print(f"\n=> LLM slice-regeneration upper bound (WRONG only) = {len(tally['WRONG'])} tasks")
    print("   HOLLOW = grammar wall (string-op), regeneration cannot cross.")


if __name__ == "__main__":
    main()
