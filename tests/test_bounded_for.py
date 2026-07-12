"""Tier-2 bounded for: ``for x in <observed collection>: <tool calls>`` compiles to
a QUANTIFIED rule -- an operand is authorized iff it matches SOME element of the
gateway-observed collection. Off-collection / tampered values are denied (FN=0)."""

from __future__ import annotations

import pytest

from pauth import prepare
from pauth.enforcer import Enforcer, check_injection, execute_generated_code
from pauth.envelope import EnvelopeStore, KeyRing
from pauth.grammar import RestrictedGrammarError
from pauth.suites.shopping import build_suite

_LOOP = '''def run():
    products = list_products(None, 100.0)
    for p in products:
        add_to_cart(p.name, 1)
'''


def _armed():
    suite = build_suite()
    prepared = prepare(_LOOP, suite.tool_names(), suite.tool_signer())
    enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), suite.tool_signer())
    execute_generated_code(
        prepared.source, enf, suite.tool_params(), suite.runner_factory(suite.make_env())
    )
    return enf


def test_bounded_for_accepted_and_compiles_to_quantified_rule():
    suite = build_suite()
    prepared = prepare(_LOOP, suite.tool_names(), suite.tool_signer())
    loop_rules = [r for r in prepared.rules if r.tool == "add_to_cart"]
    assert len(loop_rules) == 1 and loop_rules[0].loop_var == "p"


def test_membership_permits_in_collection_denies_off_collection():
    enf = _armed()
    ok = lambda args: check_injection(enf, "add_to_cart", args).permit
    assert ok(["Basic Wired Earbuds", 1])           # an element of the observed list
    assert not ok(["Aurora Noise Cancelling Headphones", 1])  # off-collection (over budget)
    assert not ok(["Hacker Product", 1])            # not in the collection -> FN=0
    assert not ok(["Basic Wired Earbuds", 50])      # quantity tampered -> no element matches


def test_index_and_range_loops_still_rejected():
    suite = build_suite()
    tools, signer = suite.tool_names(), suite.tool_signer()
    # iterate a non-variable (range / literal) -> rejected
    bad = 'def run():\n    for p in range(3):\n        add_to_cart("x", 1)\n'
    with pytest.raises(RestrictedGrammarError):
        prepare(bad, tools, signer)
    # assignment inside the loop body -> rejected
    bad2 = ('def run():\n    products = list_products(None, 100.0)\n'
            '    for p in products:\n        q = p.name\n')
    with pytest.raises(RestrictedGrammarError):
        prepare(bad2, tools, signer)
