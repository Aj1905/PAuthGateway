"""The detect-wall -> grill -> bound -> execute loop, on an unbounded-loop task.

Shows the whole path end-to-end and offline:
  1. the naive unbounded plan is a WALL (detected; grammar rejects it),
  2. the grill asks the human for the bound,
  3. the bounded plan (iterate the human-authorised, signed schedule) is
     grammar-valid, and
  4. it executes the exact schedule while DENYING every off-schedule call (FN=0).
     The bound IS the signed collection's length.
"""

from __future__ import annotations

import pytest

from pauth import prepare
from pauth.enforcer import Enforcer, check_injection, execute_generated_code
from pauth.envelope import EnvelopeStore, KeyRing
from pauth.grammar import RestrictedGrammarError
from pauth.suites import installments
from gateway.planning.bounding import detect_unbounded, is_boundable_wall


# -- 1 & 2: the unbounded plan is a wall the grill can address ----------------

def test_unbounded_plan_is_detected_as_a_wall():
    wall = detect_unbounded(installments.UNBOUNDED_CODE)
    assert wall is not None
    assert wall.construct == "while"
    assert "lease_active" in wall.condition
    q = wall.grill_question()
    assert "bound" in q.lower() and "maximum number of iterations" in q


def test_unbounded_plan_is_actually_rejected_by_the_grammar():
    # the wall is real, not just flagged: the grammar refuses `while`
    suite = installments.build_suite()
    with pytest.raises(RestrictedGrammarError):
        prepare(installments.UNBOUNDED_CODE, suite.tool_names(), suite.tool_signer())


# -- 3: the bounded (post-grill) plan is expressible --------------------------

def test_bounded_plan_has_no_wall_and_prepares():
    suite = installments.build_suite()
    task = suite.tasks[0]
    assert not is_boundable_wall(task.reference_code)      # no while left
    prepared = prepare(task.reference_code, suite.tool_names(), suite.tool_signer())
    assert prepared.rules


# -- 4: it runs the exact schedule, and off-schedule calls are denied (FN=0) --

def _executed_enforcer():
    suite = installments.build_suite()
    task = suite.tasks[0]
    prepared = prepare(task.reference_code, suite.tool_names(), suite.tool_signer())
    enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), suite.tool_signer())
    report = execute_generated_code(
        prepared.source, enf, suite.tool_params(),
        suite.runner_factory(installments.make_env()),
    )
    return suite, task, enf, report


def test_bounded_plan_pays_exactly_the_schedule():
    _suite, _task, _enf, report = _executed_enforcer()
    assert not report.crashed
    sends = [e for e in report.events if e.tool == "send_money" and e.decision.permit]
    assert len(sends) == 3  # the bound == the signed schedule length


def test_off_schedule_payments_are_denied():
    suite, task, enf, _report = _executed_enforcer()
    for inj in task.forced_injections:
        assert not check_injection(enf, inj.tool, inj.args).permit, f"{inj.tool}:{inj.args}"
    # an on-schedule payment IS authorised (availability preserved)
    assert enf.check(
        "send_money", [installments.LANDLORD_IBAN, 1200.0, "rent 2026-02", "2026-02-01"]
    ).permit
