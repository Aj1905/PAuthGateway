"""Suite-filter recall eval as a regression test (D1)."""

from eval.filter_recall import run
from tests.fixtures.filter_cases import ALL_SUITES, CASES, build_universe


def test_filter_keeps_needed_suite_with_headroom():
    universe = build_universe(ALL_SUITES)
    # With the full universe retained, nothing is dropped.
    assert run(None, universe) == []
    # top_k=2 keeps every needed suite for this corpus (recall 100%).
    assert run(2, universe) == []


def test_metric_surfaces_the_blind_spot_at_aggressive_topk():
    universe = build_universe(ALL_SUITES)
    # The metric exists to CATCH drops: top_k=1 drops at least one needed suite.
    dropped = run(1, universe)
    assert len(dropped) >= 1
    assert all(d in {c.id for c in CASES} for d in dropped)
