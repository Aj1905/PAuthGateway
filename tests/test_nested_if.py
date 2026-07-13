"""Tier-3 nested if/else: a leaf under ``if C1: if C2:`` compiles to a rule whose
guard is the CONJUNCTION ``C1 and C2`` (the enforcer already requires all guards).
Nesting only ADDS guards -- it never adds operand-value options (nested
reassignment stays rejected) -- so authorization can only get stricter and FN=0
is preserved. These probes check acceptance, the benign no-FP path, that
injections are still denied, and that the conservative bounds hold."""

from __future__ import annotations

import pytest

from pauth import prepare
from pauth.enforcer import Enforcer, check_injection, execute_generated_code
from pauth.envelope import EnvelopeStore, KeyRing
from pauth.grammar import RestrictedGrammarError
from pauth.suites.shopping import build_suite

_NESTED = '''def run():
    products = list_products(None, 100.0)
    cheapest = min(products, key=lambda p: p.price)
    if cheapest != None:
        if cheapest.price < 50.0:
            add_to_cart(cheapest.name, 1)
'''


def _armed(code):
    s = build_suite()
    prepared = prepare(code, s.tool_names(), s.tool_signer())
    enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), s.tool_signer())
    rep = execute_generated_code(
        prepared.source, enf, s.tool_params(), s.runner_factory(s.make_env())
    )
    return prepared, enf, rep


def test_nested_if_accepted_and_guard_is_conjunction():
    prepared, _, _ = _armed(_NESTED)
    rules = [r for r in prepared.rules if r.tool == "add_to_cart"]
    assert len(rules) == 1
    # the leaf carries BOTH enclosing tests as guards
    assert len(rules[0].guard) == 2, [__import__("ast").unparse(g) for g in rules[0].guard]


def test_benign_nested_run_no_fp():
    _, _, rep = _armed(_NESTED)
    assert rep.crashed is None
    assert not rep.denied, [e.decision.reason for e in rep.denied]


def test_injections_denied_under_nested_guard():
    _, enf, _ = _armed(_NESTED)
    ok = lambda t, a: check_injection(enf, t, a).permit
    # off-operand product -> matches no rule (FN=0)
    assert not ok("add_to_cart", ["Hacker Product", 1])
    # tampered quantity -> operand mismatch
    assert not ok("add_to_cart", ["Basic Wired Earbuds", 99])
    # a send_money injection -> no rule at all
    assert not ok("send_money", ["GB99EVIL", 9.0, "x", "2024-01-01"])


def test_depth_cap_rejected():
    s = build_suite()
    deep = ("def run():\n"
            "    products = list_products(None, 100.0)\n"
            "    c = min(products, key=lambda p: p.price)\n"
            "    if c != None:\n"
            "        if c.price < 90.0:\n"
            "            if c.price < 70.0:\n"
            "                if c.price < 50.0:\n"
            "                    add_to_cart(c.name, 1)\n")
    with pytest.raises(RestrictedGrammarError):
        prepare(deep, s.tool_names(), s.tool_signer())


def test_nested_reassignment_rejected():
    # x assigned at top AND in a nested branch -> multi-def not matching a blessed
    # flat merge -> rejected (keeps the fork sound).
    s = build_suite()
    code = ("def run():\n"
            "    products = list_products(None, 100.0)\n"
            "    c = min(products, key=lambda p: p.price)\n"
            "    name = \"\"\n"
            "    if c != None:\n"
            "        if c.price < 50.0:\n"
            "            name = c.name\n"
            "    add_to_cart(name, 1)\n")
    with pytest.raises(RestrictedGrammarError):
        prepare(code, s.tool_names(), s.tool_signer())


def test_for_inside_if_rejected():
    s = build_suite()
    code = ("def run():\n"
            "    products = list_products(None, 100.0)\n"
            "    if products != None:\n"
            "        for p in products:\n"
            "            add_to_cart(p.name, 1)\n")
    with pytest.raises(RestrictedGrammarError):
        prepare(code, s.tool_names(), s.tool_signer())
