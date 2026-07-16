"""Measure whether a taint-preserving deterministic structurer lifts G1 on the
banking file-reading tasks -- and whether it does so faithfully (G3).

For every banking task whose ground truth reads a file, we take each later
ground-truth argument and ask two things:

  G1 (availability): was the value prose-locked (not a prompt literal, not a
      clean field of the string read) but NOW recoverable as a typed candidate
      the structurer surfaced?
  G3 (fidelity): does the surfaced candidate EQUAL the ground-truth value
      exactly? A structurer that surfaces a *wrong* value would pass G1 but
      break G3 -- turning "inexpressible" into "expressible but wrong".

This isolates the real crux: with taint + confirmation gate handling FN=0, the
only open risk is whether the structurer reads the right value.

Run:  .venv/bin/python -m tests.experiment.structuring_probe
"""

from __future__ import annotations

import math

from agentdojo.task_suite.load_suites import get_suites

from benchmarks.agentdojo_adapter import load_suite
from pauth.structuring import structure

SUITE = "banking"


def _norm(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return round(float(v), 6)
    return str(v).strip()


def _matches_candidate(val, view) -> bool:
    """Is ``val`` present among the structurer's typed candidates (exact)?"""
    n = _norm(val)
    if isinstance(n, float):
        return any(isinstance(c, float) and math.isclose(c, n, rel_tol=1e-9)
                   for c in view.amounts)
    s = str(val).strip()
    if s in view.ibans or s in view.dates or s in view.emails:
        return True
    # free-form (e.g. a street line): only if it appears verbatim as a line
    return any(s == ln or s in ln for ln in view.lines)


def _line_only(val, view) -> bool:
    """True if recovered ONLY as a raw line (free-form), not a typed shape."""
    n = _norm(val)
    if isinstance(n, float) and any(math.isclose(c, n, rel_tol=1e-9) for c in view.amounts):
        return False
    s = str(val).strip()
    if s in view.ibans or s in view.dates or s in view.emails:
        return False
    return any(s == ln or s in ln for ln in view.lines)


def main() -> int:
    adj = get_suites("v1")[SUITE]
    spec = load_suite(SUITE)
    runner = spec.runner_factory(spec.make_env())

    print("Structuring probe -- banking file-reading tasks\n")
    print(f"{'task':<9}{'call.arg':<34}{'value':<26}{'prose?':<7}{'typed':<7}{'exact'}")
    print("-" * 95)

    n_prose = n_recovered_typed = n_recovered_line = n_exact = 0
    notes: list[str] = []

    for tid in sorted(adj.user_tasks, key=lambda k: int(k.replace("user_task_", ""))):
        ut = adj.user_tasks[tid]
        try:
            gt = ut.ground_truth(spec.make_env())
        except Exception:  # noqa: BLE001
            continue
        read_paths = [fc.args.get("file_path") for fc in gt if fc.function == "read_file"]
        if not read_paths:
            continue

        # structure every file this task reads, union the views
        texts = []
        for p in read_paths:
            try:
                texts.append(str(runner("read_file", {"file_path": p})))
            except Exception:  # noqa: BLE001
                pass
        view = structure("\n".join(texts))

        for fc in gt:
            if fc.function == "read_file":
                continue
            for key, val in fc.args.items():
                if val is None or isinstance(val, (list, dict)):
                    continue
                if str(val).strip().lower() in ut.PROMPT.lower():
                    continue  # prompt literal -- already expressible
                # this arg is prose-locked (not in prompt; the read returns a bare
                # string so it is not a clean field either)
                n_prose += 1
                typed = _matches_candidate(val, view) and not _line_only(val, view)
                line = _line_only(val, view)
                exact = _matches_candidate(val, view)
                if typed:
                    n_recovered_typed += 1
                if line:
                    n_recovered_line += 1
                if exact:
                    n_exact += 1
                tag_typed = "yes" if typed else ("line" if line else "NO")
                print(f"{tid.replace('user_task_','t'):<9}"
                      f"{fc.function + '.' + key:<34}{repr(val)[:24]:<26}"
                      f"{'yes':<7}{tag_typed:<7}{'yes' if exact else 'NO'}")

    print("-" * 95)
    print(f"\n  prose-locked args ................ {n_prose}")
    print(f"  recovered as TYPED field ......... {n_recovered_typed}"
          f"  ({100*n_recovered_typed/n_prose:.0f}% of prose-locked)" if n_prose else "")
    print(f"  recovered only as raw LINE ....... {n_recovered_line}  (free-form, weaker)")
    print(f"  exact match (fidelity / G3) ...... {n_exact}"
          f"  ({100*n_exact/n_prose:.0f}% of prose-locked)" if n_prose else "")
    print(f"  NOT recovered (needs compute/absent) {n_prose - n_recovered_typed - n_recovered_line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
