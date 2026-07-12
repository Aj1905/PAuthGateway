"""The tool-call eval harness produces the right counts + classes."""

from __future__ import annotations

from eval import metrics as M
from eval.toolcall_eval import (
    AUTHORIZED_WRITE,
    INJECTION_ATTACK,
    POST_WRITE_INTERRUPT,
    READ_ONLY,
    UNTRUSTED_UPFRONT,
    run_baseline,
    run_gateway,
)


def test_read_only_loop_has_no_side_effects_or_confirmations():
    c = run_gateway(READ_ONLY)
    assert c[M.SIDE_EFFECTING_CALLS] == 0
    assert c[M.UPFRONT_CONFIRMATIONS] == 0 and c[M.MIDRUN_INTERRUPTIONS] == 0
    assert c[M.BLOCKED_INJECTIONS] == 0


def test_authorized_write_is_not_gated():
    c = run_gateway(AUTHORIZED_WRITE)
    assert c[M.SIDE_EFFECTING_CALLS] == 1
    assert c[M.UPFRONT_CONFIRMATIONS] == 0 and c[M.MIDRUN_INTERRUPTIONS] == 0


def test_untrusted_control_before_any_write_is_upfront():
    c = run_gateway(UNTRUSTED_UPFRONT)
    assert c[M.UPFRONT_CONFIRMATIONS] == 1
    assert c[M.MIDRUN_INTERRUPTIONS] == 0  # hoistable: no write committed before the confirmation


def test_untrusted_control_after_a_write_is_a_mid_execution_interrupt():
    c = run_gateway(POST_WRITE_INTERRUPT)
    assert c[M.MIDRUN_INTERRUPTIONS] == 1  # the send_note write already ran -> flow-breaking
    assert c[M.UPFRONT_CONFIRMATIONS] == 0


def test_injection_call_is_blocked_as_a_security_win():
    c = run_gateway(INJECTION_ATTACK)
    assert c[M.BLOCKED_INJECTIONS] == 1  # the off-plan spurious transfer is denied


def test_baseline_runs_without_a_gateway():
    # Sanity: the baseline path executes the same calls with no enforcement.
    assert run_baseline(INJECTION_ATTACK) >= 0.0
