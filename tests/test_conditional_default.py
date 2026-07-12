"""Tier-2 disjunction: the "default then conditionally set" merge
(``x = <const>``; ``if C: x = <expr>``) is accepted and sliced soundly -- both
branches authorized, an off-branch (injected) value denied (FN=0 preserved)."""

from __future__ import annotations

import pytest

from pauth import prepare
from pauth.enforcer import Enforcer, check_injection, execute_generated_code
from pauth.envelope import EnvelopeStore, KeyRing
from pauth.grammar import RestrictedGrammarError
from eval.toolcall_eval import ATTACKER_IBAN, _loader

_MERGE = '''def run():
    m = read_message()
    r = "GB33BUKB20201555555555"
    if m.amount > 0.0:
        r = m.iban
    send_money(r, 10.0, "x", "2024-01-01")
'''


def _armed():
    suite = _loader("msg")
    prepared = prepare(_MERGE, suite.tool_names(), suite.tool_signer())
    enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), suite.tool_signer())
    execute_generated_code(
        prepared.source, enf, suite.tool_params(), suite.runner_factory(suite.make_env())
    )
    return enf


def test_conditional_default_merge_is_accepted():
    suite = _loader("msg")
    prepared = prepare(_MERGE, suite.tool_names(), suite.tool_signer())
    # send_money forks into two branch-rules (const default + conditional value).
    assert sum(1 for r in prepared.rules if r.tool == "send_money") == 2


def test_both_branches_authorized_offbranch_denied():
    enf = _armed()
    ok = lambda v: check_injection(enf, "send_money", [v, 10.0, "x", "2024-01-01"]).permit
    assert ok("GB33BUKB20201555555555")   # the constant default branch
    assert ok(ATTACKER_IBAN)              # the conditional branch value (m.iban)
    assert not ok("GB99WATTACKER99999999")  # off-branch injected value -> FN=0


def test_unsound_reassignment_shapes_still_rejected():
    suite = _loader("msg")
    tools, signer = suite.tool_names(), suite.tool_signer()
    # non-constant default -> rejected (needs path-merge we don't have)
    bad1 = 'def run():\n    m = read_message()\n    r = m.iban\n    if m.amount > 0.0:\n        r = m.text\n    send_money(r, 1.0, "x", "2024-01-01")\n'
    with pytest.raises(RestrictedGrammarError):
        prepare(bad1, tools, signer)
    # two conditional sets -> 3 defs -> rejected
    bad2 = 'def run():\n    m = read_message()\n    r = "GB33BUKB20201555555555"\n    if m.amount > 0.0:\n        r = m.iban\n    if m.amount > 5.0:\n        r = m.text\n    send_money(r, 1.0, "x", "2024-01-01")\n'
    with pytest.raises(RestrictedGrammarError):
        prepare(bad2, tools, signer)
