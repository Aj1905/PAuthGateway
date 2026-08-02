"""Confirmation UX (C0/C1/C2) x policy (reject/approve/human) -- offline.

The invariant under test: the UX version decides only when/how a HUMAN is
shown a held call. Under the automatic policies (reject/approve) every UX
version must produce identical execution results.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable

import pytest

from pauth.codegen import ToolDoc
from pauth.suites.base import SuiteSpec, ToolSpec

from gateway.planning.composite import CompositePlan, StageTemplate
from gateway.runtime.confirmation import SourceTrust
from gateway.runtime.confirmer import (
    RejectAllConfirmer,
    TrustingConfirmer,
    build_policy_confirmer,
)
from gateway.runtime.gateway import Gateway

ATTACKER_IBAN = "GB99EVIL00000000000000"


@dataclasses.dataclass
class Message:
    iban: str
    amount: float = 100.0


class Env:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.message = Message(iban=ATTACKER_IBAN)


_IMPL: dict[str, Callable[..., Any]] = {
    "read_message": lambda env: env.message,
    "send_money": lambda env, recipient, amount, subject, date: (
        env.sent.append({"recipient": recipient, "amount": amount}) or {"status": "ok"}
    ),
}

_TOOLS = {
    "read_message": ToolSpec(
        name="read_message", params=[], signer="src",
        doc=ToolDoc(name="read_message", description="Read an incoming message.",
                    parameters=[], returns="object {iban: string}"),
    ),
    "send_money": ToolSpec(
        name="send_money", params=["recipient", "amount", "subject", "date"], signer="bank",
        doc=ToolDoc(name="send_money", description="Send a bank transfer.",
                    parameters=[
                        {"name": "recipient", "type": "string", "desc": "recipient IBAN"},
                        {"name": "amount", "type": "number", "desc": "amount"},
                        {"name": "subject", "type": "string", "desc": "subject"},
                        {"name": "date", "type": "string", "desc": "date"},
                    ], returns="object"),
    ),
}

_CODE = (
    "def run():\n"
    "    msg = read_message()\n"
    '    send_money(msg.iban, 10.0, "Order", "2024-01-01")\n'
)
_PLAN = CompositePlan(suite_name="msg", stages=(StageTemplate(code=_CODE),))
_PROMPT = "Read my message and send 10 dollars to the IBAN it names."


def _make_env_holder():
    holder: list[Env] = []

    def make_env():
        env = Env()
        holder.append(env)
        return env

    return holder, make_env


def _gateway(ux: str, policy: str) -> tuple[Gateway, list[Env]]:
    holder, make_env = _make_env_holder()

    def loader(name):
        if name != "msg":
            raise ValueError(name)
        return SuiteSpec(
            name="msg", tools=_TOOLS, make_env=make_env,
            tool_executor_factory=lambda env: (lambda tool, kw: _IMPL[tool](env, **kw)),
            tasks=[],
        )

    gw = Gateway(
        loader,
        source_trust=SourceTrust(untrusted_tools=frozenset({"read_message"})),
        confirmation_ux=ux,
        confirmation_policy=policy,
    )
    return gw, holder


def _drive(gw: Gateway):
    assert gw.submit_user_prompt_composite(_PROMPT, _PLAN).accepted
    assert gw.handle_tool_call("read_message", []).permit
    return gw.handle_tool_call(
        "send_money", [ATTACKER_IBAN, 10.0, "Order", "2024-01-01"]
    )


# ---------------------------------------------------------- human (default) --


def test_human_policy_holds_the_call_as_before():
    gw, holder = _gateway("c1", "human")
    r = _drive(gw)
    assert not r.permit and "pending confirmation" in r.reason
    assert len(gw.pending_confirmations()) == 1
    assert holder[-1].sent == []
    # Approval then retry executes -- unchanged historical behavior.
    gw.confirm(gw.pending_confirmations()[0].confirmation_id, approved=True)
    r2 = gw.handle_tool_call("send_money", [ATTACKER_IBAN, 10.0, "Order", "2024-01-01"])
    assert r2.permit and holder[-1].sent


# ---------------------------------------------------------- automatic ---------


@pytest.mark.parametrize("ux", ["c0", "c1", "c2"])
def test_reject_policy_denies_identically_across_ux_versions(ux):
    gw, holder = _gateway(ux, "reject")
    r = _drive(gw)
    assert not r.permit
    assert "confirmation policy 'reject'" in r.reason
    assert gw.pending_confirmations() == []
    assert holder[-1].sent == []


@pytest.mark.parametrize("ux", ["c0", "c1", "c2"])
def test_approve_policy_executes_identically_across_ux_versions(ux):
    gw, holder = _gateway(ux, "approve")
    r = _drive(gw)
    assert r.permit
    assert gw.pending_confirmations() == []
    assert holder[-1].sent == [{"recipient": ATTACKER_IBAN, "amount": 10.0}]


def test_reject_policy_is_deterministic_on_retry():
    gw, holder = _gateway("c1", "reject")
    _drive(gw)
    r2 = gw.handle_tool_call("send_money", [ATTACKER_IBAN, 10.0, "Order", "2024-01-01"])
    assert not r2.permit and "confirmation policy 'reject'" in r2.reason
    assert holder[-1].sent == []


# ---------------------------------------------------------- config validity --


def test_c0_with_human_policy_is_a_config_error():
    with pytest.raises(ValueError, match="no surface"):
        _gateway("c0", "human")


def test_c2_with_human_policy_points_at_the_prototype():
    with pytest.raises(ValueError, match="not integrated"):
        _gateway("c2", "human")


def test_unknown_ux_and_policy_are_rejected():
    with pytest.raises(ValueError, match="unknown confirmation UX"):
        _gateway("c9", "reject")
    with pytest.raises(ValueError, match="unknown confirmation policy"):
        _gateway("c1", "maybe")


def test_env_defaults_preserve_historical_behavior(monkeypatch):
    monkeypatch.delenv("PAUTH_CONFIRMATION_UX", raising=False)
    monkeypatch.delenv("PAUTH_CONFIRMATION_POLICY", raising=False)
    holder, make_env = _make_env_holder()
    gw = Gateway(
        lambda name: SuiteSpec(
            name="msg", tools=_TOOLS, make_env=make_env,
            tool_executor_factory=lambda env: (lambda tool, kw: _IMPL[tool](env, **kw)),
            tasks=[],
        ),
        source_trust=SourceTrust(untrusted_tools=frozenset({"read_message"})),
    )
    r = _drive(gw)
    assert not r.permit and "pending confirmation" in r.reason


# ---------------------------------------------------------- policy -> Confirmer


def test_build_policy_confirmer_maps_policies():
    assert isinstance(build_policy_confirmer("reject"), RejectAllConfirmer)
    assert isinstance(build_policy_confirmer("approve"), TrustingConfirmer)
    with pytest.raises(ValueError, match="unknown confirmation policy"):
        build_policy_confirmer("oracle")
