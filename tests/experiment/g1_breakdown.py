"""Decompose G1 (expressibility) failures into recoverable vs not.

Question: if we add an extraction layer (reads untrusted prose, surfaces a
candidate value, human confirms), how many G1-inexpressible tasks could it
recover -- as an UPPER BOUND?

A G1 failure = some ground-truth argument value is neither a prompt literal nor
a clean scalar FIELD of a prior tool result. We split those failures:

  recoverable   -- EVERY failing value appears as a substring somewhere in the
                   reachable untrusted text (prompt + any tool result, prose
                   included). An extractor could surface it; a human confirms.
  not_recoverable -- at least one failing value appears NOWHERE in reachable
                   text -> it must be computed / synthesized / is simply absent,
                   so no extractor can produce it. (unbounded loops, string
                   synthesis, cross-field arithmetic, etc.)

recoverable is a generous UPPER bound (we let the extractor see ALL reachable
data, structured or not). If a task is not_recoverable even under that
generosity, extraction cannot help it.

Run:  .venv/bin/python -m tests.experiment.g1_breakdown
"""

from __future__ import annotations

import math
from pathlib import Path

from agentdojo.task_suite.load_suites import get_suites

from benchmarks.agentdojo_adapter import load_suite
from pauth.envelope import flatten
from pauth.evaluator import wrap

SUITES = ("banking", "slack", "travel", "workspace")


def _norm(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return round(float(v), 6)
    return str(v).strip()


def _in_pool(value, pool) -> bool:
    n = _norm(value)
    if isinstance(n, float):
        return any(isinstance(p, float) and math.isclose(p, n, rel_tol=1e-6) for p in pool)
    return n in pool


def _prompt_literal(value, prompt: str) -> bool:
    s = str(value).strip()
    if not s:
        return True
    return s.lower() in prompt.lower()


def analyze(ut, spec, params):
    """Return ('expressible'|'recoverable'|'not_recoverable', failing_args)."""
    try:
        gt = ut.ground_truth(spec.make_env())
    except Exception:  # noqa: BLE001
        return None, []
    if not gt:
        return None, []

    env = spec.make_env()
    runner = spec.runner_factory(env)
    pool: set = set()
    blob_parts: list[str] = [ut.PROMPT]
    failing: list[tuple] = []

    for fc in gt:
        for key, val in fc.args.items():
            if val is None or isinstance(val, (list, dict)):
                continue
            if _prompt_literal(val, ut.PROMPT) or _in_pool(val, pool):
                continue
            failing.append((fc.function, key, val))
        try:  # execute -> expose this call's fields (structured) + raw text (prose)
            res = runner(fc.function, dict(fc.args))
            for fv in flatten(wrap(res)).values():
                if not isinstance(fv, (list, dict)):
                    pool.add(_norm(fv))
                    blob_parts.append(str(fv))
            blob_parts.append(str(res))
        except Exception:  # noqa: BLE001
            pass

    if not failing:
        return "expressible", []

    blob = "\n".join(blob_parts).lower()
    all_in_text = all(str(val).strip().lower() in blob for _, _, val in failing)
    return ("recoverable" if all_in_text else "not_recoverable"), failing


def main() -> int:
    print("G1 breakdown -- can an extraction+human layer recover the failure?\n")
    hdr = f"{'suite':<10}{'tasks':>7}{'express':>9}{'recover':>9}{'NOT rec':>9}"
    print(hdr)
    print("-" * len(hdr))
    tot = {"tasks": 0, "expressible": 0, "recoverable": 0, "not_recoverable": 0}
    examples = {"recoverable": [], "not_recoverable": []}
    for name in SUITES:
        adj = get_suites("v1")[name]
        spec = load_suite(name)
        params = spec.tool_params()
        c = {"tasks": 0, "expressible": 0, "recoverable": 0, "not_recoverable": 0}
        for tid in sorted(adj.user_tasks):
            ut = adj.user_tasks[tid]
            verdict, failing = analyze(ut, spec, params)
            if verdict is None:
                continue
            c["tasks"] += 1
            c[verdict] += 1
            if verdict in examples and len(examples[verdict]) < 5:
                if failing:
                    f, k, v = failing[0]
                    examples[verdict].append(f"{name}/{tid}: {f}.{k}={v!r}")
        for key in tot:
            tot[key] += c[key]
        print(f"{name:<10}{c['tasks']:>7}{c['expressible']:>9}"
              f"{c['recoverable']:>9}{c['not_recoverable']:>9}")
    print("-" * len(hdr))
    print(f"{'ALL':<10}{tot['tasks']:>7}{tot['expressible']:>9}"
          f"{tot['recoverable']:>9}{tot['not_recoverable']:>9}")

    n = tot["tasks"]
    base = tot["expressible"] / n * 100
    lifted = (tot["expressible"] + tot["recoverable"]) / n * 100
    print(f"\n  G1 ceiling now (expressible / tasks) ......... {base:5.1f}%")
    print(f"  G1 ceiling WITH extraction+human (upper bound) {lifted:5.1f}%")
    print(f"  Max lift from the extraction layer ........... {lifted - base:+5.1f} pt")
    print(f"  ({tot['recoverable']} recoverable, {tot['not_recoverable']} unrecoverable "
          f"G1 failures out of {n} tasks)")

    for label in ("recoverable", "not_recoverable"):
        print(f"\n  {label} examples (first failing arg):")
        for e in examples[label]:
            print(f"    - {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
