"""Grill eval as a regression test: FN=0, no value leaks, selective approvals."""

from tests.experiment.grill_eval import _run_case
from tests.fixtures.grill_cases import CASES


def test_grill_eval_no_fn_no_leak_and_selective():
    rows = [_run_case(c) for c in CASES]
    assert all(r["status"] == "ok" for r in rows), rows
    # FN_COUNT == 0: every dangerous flow was held.
    assert sum(r["grill_fn"] for r in rows) == 0
    # VALUE_LEAK_COUNT == 0: no poisoned value reached the agent.
    assert sum(r["leak"] for r in rows) == 0
    # NUMBER_OF_APPROVAL == number of genuinely dangerous flows (perfect selectivity).
    approvals = sum(1 for r in rows if r["held"])
    dangerous = sum(1 for c in CASES if c.expected_grill)
    assert approvals == dangerous
    # Approved dangerous calls proceed.
    assert all(r["proceeds_after_approve"] for r in rows if r["held"])
