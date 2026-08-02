"""The ``sum`` reduction: deterministic aggregation inside the grammar.

The point of doing the arithmetic in-grammar (instead of handing it to an
untrusted LLM) is that the enforcer RE-DERIVES the total from the signed
collection. So:

  * the TRUE total is authorized (the plan can total a list and act on it), and
  * a FABRICATED total is off-slice and DENIED -- FN=0 is preserved by
    construction, not by a human rubber-stamp.

Offline, no API key.
"""

from __future__ import annotations

from pauth import prepare
from pauth.enforcer import Enforcer
from pauth.tool_executor import execute_generated_code
from pauth.envelope import EnvelopeStore, KeyRing
from pauth.suites import shopping

RECIPIENT = "GB33BUKB20201555555555"
SUBJECT = "bulk order"
DATE = "2026-01-29"

# Total a whole list of prices and send that sum.
PLAN = f'''\
def run():
    products = list_products(None, 1000.0)
    total = sum(products, key=lambda item: item.price)
    send_money("{RECIPIENT}", total, "{SUBJECT}", "{DATE}")
'''

# ``sum`` with no key= sums already-scalar elements directly.
PLAN_NO_KEY = '''\
def run():
    products = list_products(None, 1000.0)
    prices = len(products)
    x = sum(products, key=lambda item: item.price)
'''


def _expected_total() -> float:
    env = shopping.make_env()
    return sum(p.price for p in env.catalog.values() if p.price <= 1000.0)


def _suite():
    return shopping.build_suite()


def test_grammar_accepts_sum_with_key():
    suite = _suite()
    prepared = prepare(PLAN, suite.tool_names(), suite.tool_signer())
    assert prepared.rules


def test_sum_reduction_runs_clean_and_authorizes_true_total():
    suite = _suite()
    prepared = prepare(PLAN, suite.tool_names(), suite.tool_signer())
    enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), suite.tool_signer())
    report = execute_generated_code(
        prepared.source, enf, suite.tool_params(), suite.tool_executor_factory(shopping.make_env())
    )
    assert not report.crashed
    # send_money executed and was permitted with the re-derived sum
    sends = [e for e in report.events if e.tool == "send_money"]
    assert sends and sends[0].decision.permit

    total = _expected_total()
    # the exact total re-derives from the signed collection -> authorized
    assert enf.check("send_money", [RECIPIENT, total, SUBJECT, DATE]).permit


def test_fabricated_total_is_denied():
    suite = _suite()
    prepared = prepare(PLAN, suite.tool_names(), suite.tool_signer())
    enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), suite.tool_signer())
    execute_generated_code(
        prepared.source, enf, suite.tool_params(), suite.tool_executor_factory(shopping.make_env())
    )
    total = _expected_total()
    # any total that is not the real sum is off-slice -> default-deny (FN=0)
    for bogus in (total + 0.01, total - 50.0, 5000.0, 0.0):
        assert not enf.check("send_money", [RECIPIENT, bogus, SUBJECT, DATE]).permit
