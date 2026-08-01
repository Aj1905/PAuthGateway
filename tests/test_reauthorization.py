"""Off-plan reauthorization must be explicit, exact, and single-use."""

from __future__ import annotations

from typing import Any

import pytest

from gateway.planning.planner import PlanDraft
from gateway.runtime.gateway import Gateway
from pauth.codegen import ToolDoc
from pauth.suites.base import SuiteSpec, ToolSpec


PLAN = """\
def run():
    lookup("item-1")
    record("item-1", "fixed")
"""
PROMPT = "Look up item-1 and record the fixed value."


class _Env:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []


def _tool(name: str, params: list[str]) -> ToolSpec:
    return ToolSpec(
        name=name,
        params=params,
        signer="tiny",
        doc=ToolDoc(
            name=name,
            description=name,
            parameters=[
                {"name": param, "type": "any", "desc": param}
                for param in params
            ],
            returns="object",
        ),
    )


_TOOLS = {
    "lookup": _tool("lookup", ["key"]),
    "record": _tool("record", ["key", "value"]),
    # This tool is deployed but deliberately absent from PLAN.
    "erase": _tool("erase", ["key", "force"]),
}


class _StaticPlanner:
    def generate(self, prompt, suite_loader):
        return PlanDraft(suite_name="tiny", code=PLAN, reason="offline fixture")


def _armed() -> tuple[Gateway, _Env]:
    env = _Env()

    def runner(tool: str, kwargs: dict[str, Any]) -> Any:
        args = tuple(kwargs[param] for param in _TOOLS[tool].params)
        env.executed.append((tool, args))
        return {"ok": True}

    suite = SuiteSpec(
        name="tiny",
        tools=_TOOLS,
        make_env=lambda: env,
        runner_factory=lambda _env: runner,
        tasks=[],
    )

    def loader(name: str) -> SuiteSpec:
        if name != "tiny":
            raise ValueError(name)
        return suite

    gateway = Gateway(loader)
    result = gateway.submit_user_prompt_with_planner(
        PROMPT, _StaticPlanner(), generated_code_on_success=True
    )
    assert result.accepted
    return gateway, env


def test_off_plan_no_rule_denial_creates_one_reauthorization_request():
    gateway, env = _armed()

    first = gateway.handle_tool_call("erase", ["item-1", True])
    duplicate = gateway.handle_tool_call("erase", ["item-1", True])

    assert not first.permit and first.reauthorization_required is True
    assert not duplicate.permit and duplicate.reauthorization_required is True
    pending = gateway.pending_reauthorizations()
    assert len(pending) == 1
    assert pending[0].tool == "erase"
    assert list(pending[0].args) == ["item-1", True]
    assert pending[0].reauthorization_id
    assert env.executed == []


@pytest.mark.parametrize(
    "wrong_args",
    [
        ["item-2", True],  # a different operand
        ["item-1", 1],  # equal-by-Python, but a different concrete type
    ],
)
def test_approval_is_bound_to_all_typed_args_and_is_single_use(wrong_args):
    gateway, env = _armed()
    denied = gateway.handle_tool_call("erase", ["item-1", True])
    request_id = gateway.pending_reauthorizations()[0].reauthorization_id

    assert denied.reauthorization_required is True
    assert gateway.reauthorize(request_id, approved=True) is True

    mismatch = gateway.handle_tool_call("erase", wrong_args)
    assert not mismatch.permit
    assert env.executed == []

    exact = gateway.handle_tool_call("erase", ["item-1", True])
    replay = gateway.handle_tool_call("erase", ["item-1", True])
    assert exact.permit
    assert not replay.permit
    assert replay.reauthorization_required is True
    assert env.executed == [("erase", ("item-1", True))]


def test_rejected_reauthorization_never_executes():
    gateway, env = _armed()
    gateway.handle_tool_call("erase", ["item-1", True])
    request_id = gateway.pending_reauthorizations()[0].reauthorization_id

    assert gateway.reauthorize(request_id, approved=False) is True
    retry = gateway.handle_tool_call("erase", ["item-1", True])

    assert not retry.permit
    # Rejection is a tombstone, not a prompt loop: the same action must not
    # immediately nag the user with another pending request.
    assert retry.reauthorization_required is False
    assert gateway.pending_reauthorizations() == []
    assert env.executed == []
    assert gateway.reauthorize("missing-id", approved=True) is False


def test_float_reauthorization_is_bound_to_exact_ieee_bits():
    gateway, env = _armed()
    gateway.handle_tool_call("erase", ["item-1", -0.0])
    request_id = gateway.pending_reauthorizations()[0].reauthorization_id
    assert gateway.reauthorize(request_id, approved=True)

    positive_zero = gateway.handle_tool_call("erase", ["item-1", 0.0])
    negative_zero = gateway.handle_tool_call("erase", ["item-1", -0.0])

    assert not positive_zero.permit
    assert negative_zero.permit
    assert env.executed == [("erase", ("item-1", -0.0))]


def test_only_no_rule_denials_are_reauthorizable():
    gateway, env = _armed()

    # ``record`` is in the plan, but these operands are off-slice.  A user
    # approval must not rewrite or weaken an existing compiled rule.
    off_slice = gateway.handle_tool_call("record", ["attacker", "fixed"])
    side_channel = gateway.handle_tool_call("bash", ["echo bypass"])

    assert not off_slice.permit
    assert off_slice.reauthorization_required is False
    assert not side_channel.permit
    assert side_channel.reauthorization_required is False
    assert gateway.pending_reauthorizations() == []
    assert env.executed == []


def test_opaque_operands_fail_closed_without_a_reauthorization_request():
    gateway, env = _armed()

    result = gateway.handle_tool_call("erase", ["item-1", object()])

    assert not result.permit
    assert result.reauthorization_required is False
    assert gateway.pending_reauthorizations() == []
    assert env.executed == []
