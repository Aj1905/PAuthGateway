"""The 'trust the human' gate: a side-effecting action taken on structured-but-
untrusted data (e.g. booking the 'best' restaurant selected from an untrusted web
search) is held for human confirmation -- even though the operand is not a
recipient/amount. The narrow (S15) gate does not catch this; the broad gate does.
"""

from __future__ import annotations

from pauth import prepare
from pauth.enforcer import Enforcer, execute_generated_code
from pauth.envelope import EnvelopeStore, KeyRing
from pauth.suites.dining import build_suite

from gateway.planning.composite import CompositePlan, StageTemplate
from gateway.runtime.confirmation import SourceTrust
from gateway.runtime.feedback import ReasonCode, classify_reason
from gateway.runtime.gateway import Gateway

# search_restaurants is the UNTRUSTED source (stands in for a web page we
# extracted into structured objects); the plan books the top-rated one.
_CODE = '''def run():
    options = search_restaurants("Paris", 80.0)
    best = max(options, key=lambda r: r.rating)
    booking = book_table(best.id, "2026-08-01", 2)
    pay_deposit("GB33BUKB20201555555555", 20.0, "deposit")
'''
_PROMPT = (
    "Find the highest-rated restaurant in Paris at or under $80 per person, book "
    "a table for 2 on 2026-08-01, and pay the $20 deposit to IBAN "
    "GB33BUKB20201555555555 with reference 'deposit'."
)


def _loader(name: str):
    if name != "dining":
        raise ValueError(name)
    return build_suite()


def _real_trace() -> list[tuple[str, list]]:
    suite = build_suite()
    prepared = prepare(_CODE, suite.tool_names(), suite.tool_signer())
    enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), suite.tool_signer())
    report = execute_generated_code(
        prepared.source, enf, suite.tool_params(), suite.runner_factory(suite.make_env())
    )
    return [(e.tool, list(e.args)) for e in report.events]


def _held_tools(confirm_untrusted_decisions: bool) -> list[str]:
    gw = Gateway(_loader, source_trust=SourceTrust(
        untrusted_tools=frozenset({"search_restaurants"}),
        confirm_untrusted_decisions=confirm_untrusted_decisions,
    ))
    plan = CompositePlan(suite_name="dining", stages=(StageTemplate(code=_CODE),))
    assert gw.submit_user_prompt_composite(_PROMPT, plan).accepted
    held: list[str] = []
    for tool, args in _real_trace():
        r = gw.handle_tool_call(tool, args)
        if not r.permit and classify_reason(r.reason) == ReasonCode.PENDING_CONFIRMATION:
            held.append(tool)
            pend = gw.pending_confirmations()[-1]
            gw.confirm(pend.confirmation_id, approved=True)
            assert gw.handle_tool_call(tool, args).permit  # proceeds after approval
    return held


def test_broad_gate_holds_the_untrusted_decision_for_confirmation():
    # book_table's target was chosen from untrusted search data -> confirm.
    assert _held_tools(confirm_untrusted_decisions=True) == ["book_table"]


def test_narrow_gate_misses_the_decision_operand():
    # recipient/amount-only gate never asks about which restaurant was booked.
    assert _held_tools(confirm_untrusted_decisions=False) == []
