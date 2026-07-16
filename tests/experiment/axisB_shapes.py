"""Axis B: which CONTROL-FLOW construct does each task's canonical trace need,
and is it already in the grammar? Prioritises TCB-growing extensions by real data.

Honest caveats (the ground truth is a TRACE, not code, so structure is inferred):
  * an unbounded loop cannot be seen in a finite trace -> undercounted here.
  * a straight-line trace could hide a loop the model would write; we classify by
    the MINIMAL structure the trace demands.
So this is a PROXY for structural complexity, validated by printing examples per
bucket. It answers "how often is a control-flow construct the blocker" -- if the
answer is 'rarely', TCB-growing extensions (nested loops, filter, top-k) are low
payoff and the lever stays on axis A (structured returns).

Buckets (hardest construct the trace needs):
  straight_line   distinct tools in sequence, no repetition/aggregate   grammar OK
  bounded_loop    same tool >=2x, args from ONE prior collection         grammar OK
  reduction_sum   a scalar arg == SUM of a collection field              grammar OK (sum)
  select/agg      a scalar arg == min/max/len of a collection field      grammar OK
  join_nested     repetition crossing >=2 prior collections              BLOCKED
Run:  .venv/bin/python -m tests.experiment.axisB_shapes
"""

from __future__ import annotations

import math
from collections import Counter

from agentdojo.task_suite.load_suites import get_suites

from benchmarks.agentdojo_adapter import load_suite
from pauth.envelope import flatten
from pauth.evaluator import wrap
from eval.gates import _norm

SUITES = ("banking", "slack", "travel", "workspace")
BLOCKED = {"join_nested"}


def _collections(res_list):
    """From a list result, return {field_name: set(normalised values)} across elements."""
    fields: dict[str, set] = {}
    for el in res_list:
        for k, v in flatten(wrap(el)).items():
            if not isinstance(v, (list, dict)):
                fields.setdefault(k, set()).add(_norm(v))
    return fields


def classify(ut, spec, params):
    try:
        gt = ut.ground_truth(spec.make_env())
    except Exception:  # noqa: BLE001
        return None, None
    if not gt:
        return None, None
    runner = spec.runner_factory(spec.make_env())
    colls: list[dict[str, set]] = []      # prior list-results, each field->values
    numeric: list[tuple[str, list]] = []  # (field, [values]) for aggregate checks

    tool_counts = Counter(fc.function for fc in gt)
    repeated = {t for t, c in tool_counts.items() if c > 1}

    bucket = "straight_line"
    detail = ""

    for fc in gt:
        # which prior collections feed this call's args, and any aggregate match
        srcs = set()
        for val in fc.args.values():
            if val is None or isinstance(val, (list, dict)):
                continue
            n = _norm(val)
            for ci, fields in enumerate(colls):
                if any(n in vs for vs in fields.values()):
                    srcs.add(ci)
            # aggregate?
            if isinstance(n, (int, float)) and not isinstance(n, bool):
                for _f, vals in numeric:
                    nums = [x for x in vals if isinstance(x, (int, float))]
                    if not nums:
                        continue
                    if math.isclose(n, sum(nums), rel_tol=1e-6):
                        bucket, detail = "reduction_sum", f"{fc.function} arg=sum"
                    elif any(math.isclose(n, agg, rel_tol=1e-6)
                             for agg in (min(nums), max(nums), len(nums))):
                        if bucket == "straight_line":
                            bucket, detail = "select_agg", f"{fc.function} arg=min/max/len"
        if fc.function in repeated and len(srcs) >= 2:
            bucket, detail = "join_nested", f"{fc.function} crosses {len(srcs)} collections"
        elif fc.function in repeated and bucket in ("straight_line", "select_agg"):
            bucket, detail = "bounded_loop", f"{fc.function} x{tool_counts[fc.function]}"

        # record this call's result as a possible collection
        try:
            res = runner(fc.function, dict(fc.args))
            if isinstance(res, list) and res:
                fields = _collections(res)
                colls.append(fields)
                for f, vs in fields.items():
                    nums = [v for v in vs if isinstance(v, (int, float)) and not isinstance(v, bool)]
                    if nums:
                        numeric.append((f, nums))
        except Exception:  # noqa: BLE001
            pass
    return bucket, detail


def main() -> int:
    print("Axis B -- control-flow construct each canonical trace needs\n")
    order = ["straight_line", "select_agg", "bounded_loop", "reduction_sum", "join_nested"]
    totals: Counter = Counter()
    examples: dict[str, list] = {b: [] for b in order}
    for name in SUITES:
        adj = get_suites("v1")[name]
        spec = load_suite(name)
        params = spec.tool_params()
        for tid in sorted(adj.user_tasks):
            b, d = classify(adj.user_tasks[tid], spec, params)
            if b is None:
                continue
            totals[b] += 1
            if len(examples[b]) < 4:
                examples[b].append(f"{name}/{tid}: {d}")
    n = sum(totals.values())
    print(f"{'bucket':<16}{'count':>7}{'   grammar':>12}")
    print("-" * 36)
    for b in order:
        flag = "BLOCKED" if b in BLOCKED else "OK"
        print(f"{b:<16}{totals[b]:>7}{flag:>12}")
    print("-" * 36)
    print(f"{'TOTAL':<16}{n:>7}")
    blocked = sum(totals[b] for b in BLOCKED)
    print(f"\n  control-flow BLOCKED tasks: {blocked}/{n} ({100*blocked/n:.1f}%)")
    print(f"  grammar-OK trace shapes:    {n-blocked}/{n} ({100*(n-blocked)/n:.1f}%)")
    for b in order:
        if examples[b]:
            print(f"\n  {b}:")
            for e in examples[b]:
                print(f"    - {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
