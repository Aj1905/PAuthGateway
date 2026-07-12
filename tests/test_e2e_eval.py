"""The E2E eval (prompt -> plan -> executed trace -> gateway) is well-behaved."""

from __future__ import annotations

from eval.e2e_eval import _MSG_INTERRUPT, _MSG_UPFRONT, _shopping_tasks, run_task


def test_benign_shopping_tasks_need_no_confirmations_and_block_injections():
    tasks = _shopping_tasks()
    assert tasks, "shopping suite should expose tasks with reference code"
    for t in tasks:
        r = run_task(t)
        assert r["c_up"] == 0 and r["c_int"] == 0, t.name  # trusted source: no gate
        assert r["blocked"] == r["n_inj"], t.name          # every forced injection blocked
        assert r["a"] >= 1


def test_untrusted_control_upfront_fires_before_any_write():
    r = run_task(_MSG_UPFRONT)
    assert r["c_up"] == 1 and r["c_int"] == 0
    assert r["blocked"] == r["n_inj"]


def test_untrusted_control_after_write_is_an_interrupt():
    r = run_task(_MSG_INTERRUPT)
    assert r["c_int"] == 1 and r["c_up"] == 0
    assert r["blocked"] == r["n_inj"]
