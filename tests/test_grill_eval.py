"""Grill eval as a regression test: FN=0, no value leaks, selective approvals."""

from eval.grill_eval import _run_case
from tests.fixtures.grill_cases import CASES


def test_grill_eval_no_fn_no_leak_and_selective():
    rows = [_run_case(c) for c in CASES]
    assert all(r["status"] == "ok" for r in rows), rows
    assert sum(r["grill_fn"] for r in rows) == 0        # FN_COUNT
    assert sum(r["leak"] for r in rows) == 0            # VALUE_LEAK_COUNT
    # APPROVAL_COUNT equals the ground-truth number of confirmations (one per
    # tainted control operand) -- perfect selectivity, no over/under gating.
    approvals = sum(r["approvals"] for r in rows)
    ideal = sum(c.expected_approvals for c in CASES)
    assert approvals == ideal
    assert all(r["proceeds_after_approve"] for r in rows if r["held"])
