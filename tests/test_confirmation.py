"""Confirmation-gated sink tests (#1 closure, docs/solution.md S15/S17) -- offline.

A dangerous flow: an untrusted read (a "message" whose fields are
attacker-controlled) feeds a sink's control operand (send_money recipient). The
gateway must hold that call for user confirmation, surface the actual value on
the side channel, keep the agent-facing reason value-free (S16), and proceed
only after approval. Untrusted data reaching a CONTENT operand is not gated.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable

from pauth.codegen import ToolDoc
from pauth.suites.base import SuiteSpec, ToolSpec

from gateway.runtime.confirmation import SourceTrust, static_taint
from gateway.planning.composite import CompositePlan, StageTemplate
from gateway.planning.planner import PlanDraft
from gateway.runtime.gateway import Gateway

ATTACKER_IBAN = "GB99EVIL00000000000000"
SAFE_IBAN = "GB33BUKB20201555555555"


# ---------------------------------------------------------------------------
# A tiny suite: read_message (UNTRUSTED source) + send_money (sink) + send_note.
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Message:
    iban: str
    text: str
    amount: float = 100.0


class Env:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        # The message's fields are attacker-controlled external data.
        self.message = Message(iban=ATTACKER_IBAN, text="please pay me", amount=100.0)


def _read_message(env: Env) -> Message:
    return env.message


def _send_money(env: Env, recipient: str, amount: float, subject: str, date: str) -> dict:
    env.sent.append({"recipient": recipient, "amount": amount})
    return {"status": "ok", "recipient": recipient}


def _send_note(env: Env, recipient: str, body: str) -> dict:
    env.sent.append({"recipient": recipient, "body": body})
    return {"status": "ok"}


_IMPL: dict[str, Callable[..., Any]] = {
    "read_message": _read_message,
    "send_money": _send_money,
    "send_note": _send_note,
}

_TOOLS = {
    "read_message": ToolSpec(
        name="read_message", params=[], signer="src",
        doc=ToolDoc(name="read_message", description="Read an incoming message.",
                    parameters=[], returns="object {iban: string, text: string}"),
    ),
    "send_money": ToolSpec(
        name="send_money", params=["recipient", "amount", "subject", "date"], signer="bank",
        doc=ToolDoc(name="send_money", description="Send a bank transfer.",
                    parameters=[
                        {"name": "recipient", "type": "string", "desc": "recipient IBAN"},
                        {"name": "amount", "type": "number", "desc": "amount to transfer"},
                        {"name": "subject", "type": "string", "desc": "transfer subject"},
                        {"name": "date", "type": "string", "desc": "transfer date"},
                    ], returns="object"),
    ),
    "send_note": ToolSpec(
        name="send_note", params=["recipient", "body"], signer="msg",
        doc=ToolDoc(name="send_note", description="Send a note.",
                    parameters=[
                        {"name": "recipient", "type": "string", "desc": "recipient IBAN"},
                        {"name": "body", "type": "string", "desc": "the note body text"},
                    ], returns="object"),
    ),
}


def _suite() -> SuiteSpec:
    return SuiteSpec(
        name="msg", tools=_TOOLS, make_env=Env,
        runner_factory=lambda env: (lambda tool, kw: _IMPL[tool](env, **kw)),
        tasks=[],
    )


def _loader(name):
    if name != "msg":
        raise ValueError(name)
    return _suite()


def _gateway() -> Gateway:
    return Gateway(_loader, source_trust=SourceTrust(untrusted_tools=frozenset({"read_message"})))


# recipient derived from the untrusted message -> dangerous control-operand flow.
DERIVED_PROMPT = (
    "Read my message and send 10 dollars to the IBAN it names, "
    "subject Order, dated 2024-01-01."
)
DERIVED_CODE = '''\
def run():
    msg = read_message()
    send_money(msg.iban, 10.0, "Order", "2024-01-01")
'''
DERIVED_PLAN = CompositePlan(suite_name="msg", stages=(StageTemplate(code=DERIVED_CODE),))


def _drive(gw):
    assert gw.submit_user_prompt_composite(DERIVED_PROMPT, DERIVED_PLAN).accepted
    assert gw.handle_tool_call("read_message", []).permit
    return gw.handle_tool_call("send_money", [ATTACKER_IBAN, 10.0, "Order", "2024-01-01"])


# ---------------------------------------------------------------------------
# Static provenance taint (S20)
# ---------------------------------------------------------------------------

_DOCS = {name: spec.doc for name, spec in _TOOLS.items()}
_TRUST = SourceTrust(untrusted_tools=frozenset({"read_message"}))


def test_static_taint_flags_derived_control_operand():
    gated = static_taint(DERIVED_CODE, _DOCS, _TRUST)
    assert ("send_money", 0) in gated  # recipient = msg.iban
    assert ("send_money", 1) not in gated  # amount is a constant


def test_static_taint_survives_transformation_laundering():
    # amount = msg.amount * 2 -- a value-matching taint would MISS this; the
    # provenance taint must still flag the amount operand.
    laundered = (
        "def run():\n"
        "    msg = read_message()\n"
        '    send_money("GB33BUKB20201555555555", msg.amount * 2, "x", "2024-01-01")\n'
    )
    docs = dict(_DOCS)
    # read_message doc has no amount field, but expr provenance only needs the
    # variable to trace back to read_message.
    gated = static_taint(laundered, docs, _TRUST)
    assert ("send_money", 1) in gated  # laundered amount still tainted


def test_static_taint_empty_without_labels():
    assert static_taint(DERIVED_CODE, _DOCS, SourceTrust()) == set()


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def test_untrusted_derived_recipient_is_held_for_confirmation():
    gw = _gateway()
    result = _drive(gw)
    assert not result.permit
    pend = gw.pending_confirmations()
    assert len(pend) == 1
    # The human side channel sees the ACTUAL (poisoned) value.
    assert pend[0].value == ATTACKER_IBAN
    assert pend[0].tool == "send_money"


def test_agent_reason_on_pending_is_value_free():
    gw = _gateway()
    result = _drive(gw)
    assert not result.permit
    assert result.agent_reason is not None
    assert ATTACKER_IBAN not in result.agent_reason  # poisoned value never reaches the agent


def test_call_proceeds_after_approval():
    gw = _gateway()
    result = _drive(gw)
    assert not result.permit
    cid = gw.pending_confirmations()[0].confirmation_id
    assert gw.confirm(cid, approved=True)
    retry = gw.handle_tool_call("send_money", [ATTACKER_IBAN, 10.0, "Order", "2024-01-01"])
    assert retry.permit


def test_call_stays_denied_after_rejection():
    gw = _gateway()
    _drive(gw)
    cid = gw.pending_confirmations()[0].confirmation_id
    assert gw.confirm(cid, approved=False)
    retry = gw.handle_tool_call("send_money", [ATTACKER_IBAN, 10.0, "Order", "2024-01-01"])
    assert not retry.permit


def test_retry_while_pending_does_not_spawn_duplicate_requests():
    gw = _gateway()
    _drive(gw)
    gw.handle_tool_call("send_money", [ATTACKER_IBAN, 10.0, "Order", "2024-01-01"])
    assert len(gw.pending_confirmations()) == 1


def test_no_gate_without_source_trust_labels():
    # Same plan, but read_message is not labelled untrusted -> nothing tainted.
    gw = Gateway(_loader)  # default SourceTrust() = empty
    result = _drive(gw)
    assert result.permit
    assert gw.pending_confirmations() == []


# ---------------------------------------------------------------------------
# content vs control (S15): untrusted data in a CONTENT operand is not gated.
# ---------------------------------------------------------------------------

CONTENT_PROMPT = (
    "Read my message and forward its text to IBAN GB33BUKB20201555555555."
)
CONTENT_CODE = '''\
def run():
    msg = read_message()
    send_note("GB33BUKB20201555555555", msg.text)
'''
CONTENT_PLAN = CompositePlan(suite_name="msg", stages=(StageTemplate(code=CONTENT_CODE),))


def test_untrusted_data_in_content_operand_is_not_gated():
    gw = _gateway()
    assert gw.submit_user_prompt_composite(CONTENT_PROMPT, CONTENT_PLAN).accepted
    assert gw.handle_tool_call("read_message", []).permit
    # recipient is a trusted constant (in prompt); the tainted text goes to the
    # CONTENT operand (body), which is not gated.
    result = gw.handle_tool_call("send_note", [SAFE_IBAN, "please pay me"])
    assert result.permit
    assert gw.pending_confirmations() == []


# ---------------------------------------------------------------------------
# S19: the gate must fire on the LIVE session path (freeform / recognizer),
# not only the composite path -- this is the unification that closes #1 for
# real prompts.
# ---------------------------------------------------------------------------

class _StubPlanner:
    """Stand-in for the freeform/recognizer planners: emits a single run()."""

    def __init__(self, code):
        self.code = code

    def generate(self, prompt, suite_loader):
        return PlanDraft(suite_name="msg", code=self.code, reason="stub")


def test_gate_fires_on_session_path():
    # A single-run() plan submitted through the session path (as freeform does),
    # NOT the composite path.
    gw = _gateway()
    sub = gw.submit_user_prompt_with_planner(DERIVED_PROMPT, _StubPlanner(DERIVED_CODE))
    assert sub.accepted, sub.reason
    assert gw._composite is None  # confirm we are on the session path
    assert gw.handle_tool_call("read_message", []).permit
    held = gw.handle_tool_call("send_money", [ATTACKER_IBAN, 10.0, "Order", "2024-01-01"])
    assert not held.permit
    assert held.agent_reason is not None
    assert ATTACKER_IBAN not in held.agent_reason
    pend = gw.pending_confirmations()
    assert len(pend) == 1 and pend[0].value == ATTACKER_IBAN


def test_session_path_proceeds_after_approval():
    gw = _gateway()
    gw.submit_user_prompt_with_planner(DERIVED_PROMPT, _StubPlanner(DERIVED_CODE))
    gw.handle_tool_call("read_message", [])
    assert not gw.handle_tool_call("send_money", [ATTACKER_IBAN, 10.0, "Order", "2024-01-01"]).permit
    cid = gw.pending_confirmations()[0].confirmation_id
    assert gw.confirm(cid, approved=True)
    retry = gw.handle_tool_call("send_money", [ATTACKER_IBAN, 10.0, "Order", "2024-01-01"])
    assert retry.permit


def test_session_path_no_labels_no_gate():
    gw = Gateway(_loader)  # no source_trust
    gw.submit_user_prompt_with_planner(DERIVED_PROMPT, _StubPlanner(DERIVED_CODE))
    gw.handle_tool_call("read_message", [])
    result = gw.handle_tool_call("send_money", [ATTACKER_IBAN, 10.0, "Order", "2024-01-01"])
    assert result.permit  # nothing tainted -> unchanged behaviour


def test_laundered_amount_is_gated_end_to_end():
    # The under-gate hole (#2): a transformed untrusted amount must still gate.
    laundered_code = (
        "def run():\n"
        "    msg = read_message()\n"
        '    send_money("GB33BUKB20201555555555", msg.amount * 2, "Order", "2024-01-01")\n'
    )
    prompt = (
        "Read my message and send twice the amount it names to "
        "IBAN GB33BUKB20201555555555, subject Order, dated 2024-01-01."
    )
    gw = _gateway()
    assert gw.submit_user_prompt_with_planner(prompt, _StubPlanner(laundered_code)).accepted
    gw.handle_tool_call("read_message", [])
    held = gw.handle_tool_call("send_money", [SAFE_IBAN, 200.0, "Order", "2024-01-01"])
    assert not held.permit  # amount is untrusted-derived despite the * 2


# ---------------------------------------------------------------------------
# #3: fail-closed default.
# ---------------------------------------------------------------------------

def test_fail_closed_gates_unlabelled_source():
    # No explicit label for read_message, but fail-closed => it is untrusted,
    # so its derived recipient is gated. Own-data reads would be declared
    # trusted; here there are none.
    gw = Gateway(_loader, source_trust=SourceTrust.fail_closed())
    gw.submit_user_prompt_with_planner(DERIVED_PROMPT, _StubPlanner(DERIVED_CODE))
    gw.handle_tool_call("read_message", [])
    held = gw.handle_tool_call("send_money", [ATTACKER_IBAN, 10.0, "Order", "2024-01-01"])
    assert not held.permit


def test_fail_closed_respects_trusted_declaration():
    # read_message declared trusted -> its derived recipient is NOT gated.
    gw = Gateway(
        _loader,
        source_trust=SourceTrust.fail_closed(trusted_tools={"read_message", "send_money"}),
    )
    gw.submit_user_prompt_with_planner(DERIVED_PROMPT, _StubPlanner(DERIVED_CODE))
    gw.handle_tool_call("read_message", [])
    result = gw.handle_tool_call("send_money", [ATTACKER_IBAN, 10.0, "Order", "2024-01-01"])
    assert result.permit
