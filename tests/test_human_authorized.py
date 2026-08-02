"""Human-authorization path: a human holds FN=0 for actions the enforcer must deny,
via single-use, fully-bound grants. These tests pin the two load-bearing conditions:

  1. meaningful confirmation -- a rubber-stamp human loses FN=0, an informed one keeps it;
  2. grant binding -- one approval permits EXACTLY one call: a replay finds no grant
     (single use), and an operand-splice finds no grant (all control operands bound),
     and a forged grant fails signature verification.

Offline, no API key.
"""
from __future__ import annotations

from pauth import prepare
from pauth.codegen import ToolDoc
from pauth.enforcer import Enforcer
from pauth.envelope import EnvelopeStore, KeyRing
from pauth.suites.base import SuiteSpec, ToolSpec
from gateway.runtime.confirmer import OracleConfirmer, TrustingConfirmer
from gateway.runtime.human_authorized import (
    GrantLedger, HumanGrant, ProposedAction, authorize_proposals,
    execute_with_human_authorization, gate_agent_stream, redeem_and_execute,
)


class PayEnv:
    def __init__(self):
        self.a, self.b = 100.0, 200.0
        self.paid: list = []


def _pay_tool_executor(env):
    def run(tool, kwargs):
        if tool == "read_bills":
            return {"a": env.a, "b": env.b}
        if tool == "pay":
            env.paid.append((kwargs["recipient"], kwargs["amount"]))
            return {"ok": True}
        raise KeyError(tool)
    return run


def _tool(name, params, ret):
    return ToolSpec(name=name, params=params, signer="s",
                    doc=ToolDoc(name=name, description=name,
                                parameters=[{"name": p, "type": "string", "desc": p} for p in params],
                                returns=ret))


_TOOLS = {
    "read_bills": _tool("read_bills", [], "object {a: number, b: number}"),
    "pay": _tool("pay", ["recipient", "amount"], "object {ok: boolean}"),
}
_DOCS = {n: s.doc for n, s in _TOOLS.items()}

# the plan pays alice but MISSES bob -- the data-asymmetry deficiency
PLAN = 'def run():\n    bills = read_bills()\n    pay("alice", bills.a)\n'


def _armed():
    suite = SuiteSpec(name="p", tools=_TOOLS, make_env=PayEnv,
                      tool_executor_factory=_pay_tool_executor, tasks=[])
    prepared = prepare(PLAN, suite.tool_names(), suite.tool_signer())
    env = suite.make_env()
    enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), suite.tool_signer())
    return prepared, enf, env, suite


def test_approved_proposal_executes_and_plan_calls_unchanged():
    prepared, enf, env, suite = _armed()
    proposer = type("P", (), {"propose": lambda self: [
        ProposedAction("pay", ["bob", 200.0], sources=("read_email",))]})()
    rep = execute_with_human_authorization(
        prepared.source, enf, suite.tool_params(), suite.tool_executor_factory(env),
        proposer=proposer, confirmer=TrustingConfirmer(), docs=_DOCS)
    # the enforcer path still ran the plan's own call, and the human recovered bob
    assert ("alice", 100.0) in env.paid
    assert ("bob", 200.0) in env.paid
    assert len(rep.human_authorized) == 1
    assert rep.crashed is None


def test_single_use_replay_denied():
    """One approval -> one grant. A replayed identical call finds no unconsumed grant."""
    ledger = GrantLedger()
    approved, _ = authorize_proposals(
        [ProposedAction("pay", ["bob", 200.0])],
        confirmer=TrustingConfirmer(), ledger=ledger, docs=_DOCS,
        tool_params={"pay": ["recipient", "amount"]})
    env = PayEnv()
    # executor sees the approved call PLUS an injected replay of it
    stream = [ProposedAction("pay", ["bob", 200.0]), ProposedAction("pay", ["bob", 200.0])]
    executed, denied, _ = redeem_and_execute(
        stream, ledger=ledger, docs=_DOCS,
        tool_params={"pay": ["recipient", "amount"]}, tool_executor=_pay_tool_executor(env))
    assert len(executed) == 1                 # only one redemption succeeds
    assert len(denied) == 1                   # the replay is denied (FN=0 on reuse)
    assert env.paid == [("bob", 200.0)]


def test_operand_splice_denied():
    """A grant bound to (landlord, 98.70) does NOT authorize (attacker, 98.70)."""
    ledger = GrantLedger()
    authorize_proposals(
        [ProposedAction("pay", ["landlord", 98.70])],
        confirmer=TrustingConfirmer(), ledger=ledger, docs=_DOCS,
        tool_params={"pay": ["recipient", "amount"]})
    env = PayEnv()
    executed, denied, _ = redeem_and_execute(
        [ProposedAction("pay", ["attacker", 98.70])],   # reuse the blessed amount, new recipient
        ledger=ledger, docs=_DOCS,
        tool_params={"pay": ["recipient", "amount"]}, tool_executor=_pay_tool_executor(env))
    assert executed == []
    assert len(denied) == 1
    assert env.paid == []                     # nothing sent to the attacker


def test_forged_grant_rejected():
    """A grant with a signature the authority did not produce fails redemption."""
    ledger = GrantLedger()
    forged = HumanGrant("pay", ("attacker", "500.0"), nonce="deadbeef", signature="0" * 64)
    ledger._live.append(forged)
    env = PayEnv()
    executed, denied, _ = redeem_and_execute(
        [ProposedAction("pay", ["attacker", 500.0])],
        ledger=ledger, docs=_DOCS,
        tool_params={"pay": ["recipient", "amount"]}, tool_executor=_pay_tool_executor(env))
    assert executed == []
    assert len(denied) == 1


def test_rubber_stamp_loses_fn0_informed_keeps_it():
    """Condition 1: the SAME injected proposal is approved by a rubber-stamp human
    (FN) but rejected by an informed one (FN=0)."""
    injection = ProposedAction("pay", ["attacker", 200.0], sources=("read_email",))

    # rubber-stamp: approves the injection -> a grant is minted (FN surface)
    l1 = GrantLedger()
    approved, _ = authorize_proposals([injection], confirmer=TrustingConfirmer(),
                                      ledger=l1, docs=_DOCS,
                                      tool_params={"pay": ["recipient", "amount"]})
    assert len(approved) == 1

    # informed human who knows the benign recipient rejects the tampered value
    l2 = GrantLedger()
    approved2, rejected2 = authorize_proposals(
        [injection], confirmer=OracleConfirmer(expected="bob"),
        ledger=l2, docs=_DOCS, tool_params={"pay": ["recipient", "amount"]})
    assert approved2 == []
    assert len(rejected2) == 1


# ---- runtime "read gmail, THEN act" stream: the confirmation gate confirms the
#      whole ACTION (not just an operand), for an action the enforcer must deny ----

_STREAM_TOOLS = {
    "read_email": _tool("read_email", [], "object {body: string}"),
    "send_money": _tool("send_money", ["recipient", "amount"], "object {ok: boolean}"),
}
_STREAM_DOCS = {n: s.doc for n, s in _STREAM_TOOLS.items()}
EMPTY_PLAN = "def run():\n    pass\n"   # static Planner produced no plan for the action


def _stream():
    suite = SuiteSpec(name="s", tools=_STREAM_TOOLS, make_env=lambda: None,
                      tool_executor_factory=lambda e: None, tasks=[])
    prepared = prepare(EMPTY_PLAN, suite.tool_names(), suite.tool_signer())
    enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), suite.tool_signer())
    sent: list = []

    def tool_executor(tool, kwargs):
        if tool == "read_email":
            return {"body": "please pay bob 200"}
        if tool == "send_money":
            sent.append((kwargs["recipient"], kwargs["amount"]))
            return {"ok": True}
        raise KeyError(tool)
    return enf, suite, tool_executor, sent


def test_gmail_follow_read_then_confirm_action_executes():
    """Agent reads gmail, THEN determines send_money (off-plan). An informed human
    approves the whole action -> it runs, with the untrusted source as provenance."""
    enf, suite, tool_executor, sent = _stream()
    stream = [("read_email", []), ("send_money", ["bob", 200.0])]
    rep = gate_agent_stream(stream, enf, suite.tool_params(), tool_executor,
                            confirmer=OracleConfirmer(expected="bob"), docs=_STREAM_DOCS)
    assert sent == [("bob", 200.0)]                 # the determined action ran after confirmation
    assert len(rep.human_authorized) == 1
    assert rep.reads and rep.reads[0][0] == "read_email"
    assert rep.rejected == []


def test_gmail_follow_informed_human_rejects_injection():
    """A fabricated action (injection in the gmail) is rejected by an informed human."""
    enf, suite, tool_executor, sent = _stream()
    stream = [("read_email", []), ("send_money", ["attacker", 9999.0])]
    rep = gate_agent_stream(stream, enf, suite.tool_params(), tool_executor,
                            confirmer=OracleConfirmer(expected="bob"), docs=_STREAM_DOCS)
    assert sent == []                                # nothing sent to the attacker
    assert len(rep.rejected) == 1


def test_gmail_follow_rubber_stamp_lets_injection_through():
    """Condition 1: a rubber-stamp human forfeits FN=0 on the fabricated action."""
    enf, suite, tool_executor, sent = _stream()
    stream = [("read_email", []), ("send_money", ["attacker", 9999.0])]
    rep = gate_agent_stream(stream, enf, suite.tool_params(), tool_executor,
                            confirmer=TrustingConfirmer(), docs=_STREAM_DOCS)
    assert sent == [("attacker", 9999.0)]            # FN -- the human is the only backstop
