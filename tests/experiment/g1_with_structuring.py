"""Does the structuring layer raise the MEASURED G1 ceiling? (deterministic)

G1 (expressibility) is A1-independent: gate1_expressible asks whether every
ground-truth argument is a prompt literal or a clean FIELD of a prior tool
result. A prose-locked value (an amount inside a text blob) fails, because
read_file returns a bare string with no such field.

This re-runs the exact gate1 oracle, but with one change in the AUGMENTED pass:
whenever a prior result carries text, we also run the deterministic structurer
(pauth.structuring.structure) over it and add its typed candidates
(amounts / ibans / dates / emails) -- and, optionally, the SUM of the amounts --
to the pool. That models a plan calling ``structure_text`` on the read and
referencing ``view.amounts`` / ``sum(view.amounts)``.

So the delta is the G1 CEILING lift the structuring + sum machinery makes
possible -- with NO A1 regeneration and NO generation noise. (Whether A1 would
actually emit such plans is the separate, noisier question.)

Run:  .venv/bin/python -m tests.experiment.g1_with_structuring
"""

from __future__ import annotations

from agentdojo.task_suite.load_suites import get_suites

from benchmarks.agentdojo_adapter import load_suite
from pauth.envelope import flatten
from pauth.evaluator import wrap
from pauth.structuring import structure
from eval.gates import _in_pool, _norm, _prompt_literal

SUITES = ("banking", "slack", "travel", "workspace")


def _augment_pool(res, pool, use_sum: bool) -> None:
    """Add the deterministic structurer's typed candidates for any text in res."""
    text = str(res)
    view = structure(text)
    for c in (*view.amounts, *view.ibans, *view.dates, *view.emails):
        pool.add(_norm(c))
    if use_sum and view.amounts:
        pool.add(_norm(sum(view.amounts)))


def g1(ut, spec, params, *, structuring: bool, use_sum: bool) -> bool | None:
    try:
        gt = ut.ground_truth(spec.make_env())
    except Exception:  # noqa: BLE001
        return None
    if not gt:
        return None
    runner = spec.runner_factory(spec.make_env())
    pool: set = set()
    for fc in gt:
        for _key, val in fc.args.items():
            if val is None or isinstance(val, (list, dict)):
                continue
            if _prompt_literal(val, ut.PROMPT) or _in_pool(val, pool):
                continue
            return False
        try:
            res = runner(fc.function, dict(fc.args))
            for fv in flatten(wrap(res)).values():
                if not isinstance(fv, (list, dict)):
                    pool.add(_norm(fv))
            if structuring:
                _augment_pool(res, pool, use_sum)
        except Exception:  # noqa: BLE001
            pass
    return True


def _rate(tasks, **kw) -> tuple[int, int]:
    ok = considered = 0
    for ut, spec, params in tasks:
        v = g1(ut, spec, params, **kw)
        if v is None:
            continue
        considered += 1
        ok += 1 if v else 0
    return ok, considered


def main() -> int:
    print("G1 ceiling: baseline vs structuring vs structuring+sum (deterministic)\n")
    hdr = f"{'suite':<10}{'baseline':>12}{'+structuring':>14}{'+struct+sum':>13}"
    print(hdr)
    print("-" * len(hdr))
    tot = {"base": [0, 0], "struct": [0, 0], "sum": [0, 0]}
    for name in SUITES:
        adj = get_suites("v1")[name]
        spec = load_suite(name)
        params = spec.tool_params()
        tasks = [(adj.user_tasks[t], spec, params) for t in sorted(adj.user_tasks)]
        b = _rate(tasks, structuring=False, use_sum=False)
        s = _rate(tasks, structuring=True, use_sum=False)
        sm = _rate(tasks, structuring=True, use_sum=True)
        for key, r in (("base", b), ("struct", s), ("sum", sm)):
            tot[key][0] += r[0]
            tot[key][1] += r[1]
        print(f"{name:<10}{f'{b[0]}/{b[1]}':>12}{f'{s[0]}/{s[1]}':>14}{f'{sm[0]}/{sm[1]}':>13}")
    print("-" * len(hdr))
    b, s, sm = tot["base"], tot["struct"], tot["sum"]
    print(f"{'ALL':<10}{f'{b[0]}/{b[1]}':>12}{f'{s[0]}/{s[1]}':>14}{f'{sm[0]}/{sm[1]}':>13}")
    n = b[1]
    print(f"\n  baseline G1 ............. {100*b[0]/n:.1f}%")
    print(f"  + structuring .......... {100*s[0]/n:.1f}%  ({s[0]-b[0]:+d} tasks)")
    print(f"  + structuring + sum .... {100*sm[0]/n:.1f}%  ({sm[0]-b[0]:+d} tasks vs baseline)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
