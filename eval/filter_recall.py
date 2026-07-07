"""Suite-filter recall eval (D1): does the filter drop a needed suite?

Metric: FILTER_DROP_COUNT (needed suite dropped by the filter) and FILTER_RECALL
over the labelled corpus, swept across top_k. A small top_k shrinks A1's prompt
but risks dropping the suite a task needs -- this quantifies that blind spot.

Run: .venv/bin/python -m eval.filter_recall
"""
from __future__ import annotations

import sys

from gateway.providers.suite_filter import SuiteFilter
from tests.fixtures.filter_cases import ALL_SUITES, CASES, build_universe


def run(top_k, universe):
    f = SuiteFilter(top_k=top_k)
    dropped = []
    for c in CASES:
        result = f.filter(c.prompt, universe)
        if c.needed_suite not in result.selected:
            dropped.append(c.id)
    return dropped


def main() -> int:
    universe = build_universe(ALL_SUITES)
    total = len(CASES)
    print("=" * 66)
    print(f"FILTER recall eval -- {total} prompts over {len(universe)} suites")
    print("=" * 66)
    print(f"{'top_k':<8}{'FILTER_DROP_COUNT':<20}{'FILTER_RECALL':<16}dropped")
    print("-" * 66)
    worst = 0
    for top_k in (1, 2, 3, None):
        dropped = run(top_k, universe)
        recall = (total - len(dropped)) / total
        worst = max(worst, len(dropped))
        label = str(top_k) if top_k is not None else "all"
        print(f"{label:<8}{len(dropped):<20}{recall:<16.0%}{', '.join(dropped) or '-'}")
    # A deployment picks top_k; the eval just exposes the recall/size tradeoff.
    print("\nNote: FILTER_DROP_COUNT > 0 at small top_k means a needed suite was")
    print("dropped -- the blind spot this metric exists to surface.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
