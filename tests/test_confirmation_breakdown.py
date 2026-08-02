"""A computed (reduced) operand's confirmation surfaces a LABELLED breakdown
table -- item -> amount -- from the same signed envelopes the enforcer used, so
the human can read WHAT each amount is for and catch an injected line. An amount
whose purpose could not be read is shown as 不明 (unknown) -- deliberately
suspicious.

Offline, no API key.
"""

from __future__ import annotations

from pauth import prepare
from pauth.enforcer import Enforcer, execute_generated_code
from pauth.envelope import EnvelopeStore, KeyRing
from pauth.structuring import UNKNOWN_LABEL
from pauth.suites import websum
from gateway.runtime.confirmation import (
    BreakdownRow,
    PendingConfirmation,
    reduction_breakdown,
)


def _executed_enforcer(env=None):
    suite = websum.build_suite()
    task = suite.tasks[0]
    prepared = prepare(task.reference_code, suite.tool_names(), suite.tool_signer())
    enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), suite.tool_signer())
    execute_generated_code(
        prepared.source, enf, suite.tool_params(),
        suite.tool_executor_factory(env or websum.make_env()),
    )
    send_rule = next(r for r in prepared.rules if r.tool == "send_money")
    return enf, send_rule


def test_reduction_breakdown_surfaces_labelled_rows():
    enf, send_rule = _executed_enforcer()
    op, rows = reduction_breakdown(send_rule, 1, enf.store)  # amount operand
    assert op == "sum"
    assert sorted(r.value for r in rows) == [12.99, 45.5, 120.0]
    assert {r.label for r in rows} == {"Design work", "Managed hosting", "Domain registration"}


def test_literal_operand_has_no_breakdown():
    enf, send_rule = _executed_enforcer()
    # recipient (index 0) is a prompt literal, not a reduction -> no breakdown
    assert reduction_breakdown(send_rule, 0, enf.store) is None


def test_human_warning_renders_the_table():
    conf = PendingConfirmation(
        "c0", "send_money", 1, "amount", 178.49, source=("read_site",),
        breakdown=("sum", (BreakdownRow("Design work", 120.0),
                           BreakdownRow("Managed hosting", 45.5),
                           BreakdownRow("Domain registration", 12.99))),
    )
    w = conf.human_warning()
    assert "the sum of" in w
    for token in ("Design work", "120", "45.5", "12.99", "check EACH row"):
        assert token in w


def test_unattributed_injected_amount_shows_as_unknown():
    # an injected amount with no readable item -> 不明, visible & suspicious.
    env = websum.make_env()
    env.sites["https://vendor.example/invoice"] = (
        "Invoice from ACME\n"
        "  - Design work ......... 120.00\n"
        "  - Managed hosting ..... 45.50\n"
        "  - Domain registration . 12.99\n"
        "  ......................... 500.00\n"   # no item -> 不明
    )
    enf, send_rule = _executed_enforcer(env)
    op, rows = reduction_breakdown(send_rule, 1, enf.store)
    unknown = [r for r in rows if r.value == 500.0]
    assert unknown and unknown[0].label == UNKNOWN_LABEL
    conf = PendingConfirmation("c0", "send_money", 1, "amount", 678.49,
                               source=("read_site",), breakdown=(op, rows))
    assert UNKNOWN_LABEL in conf.human_warning()
