"""The E2E eval (prompt -> plan -> executed trace -> gateway) is well-behaved."""

from __future__ import annotations

from eval import metrics as M
from eval.e2e_eval import _MSG_INTERRUPT, _MSG_UPFRONT, _shopping_tasks, run_task


def test_benign_shopping_tasks_need_no_confirmations_and_block_injections():
    tasks = _shopping_tasks()
    assert tasks, "shopping suite should expose tasks with reference code"
    for t in tasks:
        r = run_task(t)
        assert r[M.UPFRONT_CONFIRMATIONS] == 0 and r[M.MIDRUN_INTERRUPTIONS] == 0, t.name  # trusted source: no gate
        assert r[M.BLOCKED_INJECTIONS] == r[M.TOTAL_INJECTIONS], t.name                    # every forced injection blocked
        assert r[M.TOTAL_TOOL_CALLS] >= 1


def test_untrusted_control_upfront_fires_before_any_write():
    r = run_task(_MSG_UPFRONT)
    assert r[M.UPFRONT_CONFIRMATIONS] == 1 and r[M.MIDRUN_INTERRUPTIONS] == 0
    assert r[M.BLOCKED_INJECTIONS] == r[M.TOTAL_INJECTIONS]


def test_untrusted_control_after_write_is_an_interrupt():
    r = run_task(_MSG_INTERRUPT)
    assert r[M.MIDRUN_INTERRUPTIONS] == 1 and r[M.UPFRONT_CONFIRMATIONS] == 0
    assert r[M.BLOCKED_INJECTIONS] == r[M.TOTAL_INJECTIONS]
