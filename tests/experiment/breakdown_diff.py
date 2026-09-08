"""Compare two ``p4_breakdown --json`` outputs task by task.

Usage: .venv/bin/python -m tests.experiment.breakdown_diff base.json treat.json

Prints which tasks flipped exact<->not, and the missing/excess tool deltas, so
a treatment's effect can be read as "fixed X, broke Y" rather than one number.
"""
from __future__ import annotations

import json
import sys
from collections import Counter


def _load(path):
    rows = json.load(open(path))
    return {f"{r['suite']}/{r['task']}": r for r in rows}


def main() -> int:
    base, treat = _load(sys.argv[1]), _load(sys.argv[2])
    fixed, broken, same_bad = [], [], []
    for key in sorted(set(base) | set(treat)):
        b = base.get(key, {}).get("status")
        t = treat.get(key, {}).get("status")
        if b != "exact" and t == "exact":
            fixed.append((key, b))
        elif b == "exact" and t != "exact":
            broken.append((key, t))
        elif b != "exact" and t != "exact":
            same_bad.append(key)
    print(f"exact base={sum(1 for r in base.values() if r['status']=='exact')} "
          f"treat={sum(1 for r in treat.values() if r['status']=='exact')}  "
          f"fixed={len(fixed)} broken={len(broken)} still_failing={len(same_bad)}")
    print("\nFIXED (base status):")
    for k, b in fixed:
        print(f"  {k} [{b}]")
    print("\nBROKEN (treat status):")
    for k, t in broken:
        print(f"  {k} [{t}]")
        r = treat[k]
        for m in r.get("missing", []):
            print("     MISSING", m["kind"], m["tool"])
        for e in r.get("excess", []):
            print("     EXCESS ", e["kind"], e["tool"])

    def counts(rows, field):
        c = Counter()
        for r in rows.values():
            for x in r.get(field, []):
                c[x["tool"]] += 1
        return c
    for field in ("missing", "excess"):
        cb, ct = counts(base, field), counts(treat, field)
        delta = {k: ct[k] - cb[k] for k in set(cb) | set(ct) if ct[k] != cb[k]}
        print(f"\n{field.upper()} delta by tool (treat - base):",
              dict(sorted(delta.items(), key=lambda kv: kv[1])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
