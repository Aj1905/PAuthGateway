"""Compiler-derived execution-plan contract tests.

These tests are deliberately offline.  The contract must be a deterministic
projection of the normalized PAuth program, not a second LLM-authored plan.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from gateway.planning.planner import PlanDraft
from gateway.runtime.gateway import Gateway
from pauth import prepare
from pauth.codegen import ToolDoc
from pauth.suites.base import SuiteSpec, ToolSpec


SOURCE_A = """\
def run():
    item = read_item("item-1")
    publish_item(item.owner, "ready")
"""

SOURCE_B = """\
def run( ):

    item=read_item( "item-1" )
    publish_item( item.owner,"ready" )
"""

TOOL_NAMES = {"read_item", "publish_item", "unused_tool"}
TOOL_SIGNERS = {
    "read_item": "reader",
    "publish_item": "writer",
    "unused_tool": "unused",
}


def test_execution_plan_is_deterministic_for_the_same_normalized_code():
    first = prepare(SOURCE_A, TOOL_NAMES, TOOL_SIGNERS)
    second = prepare(SOURCE_B, TOOL_NAMES, TOOL_SIGNERS)

    assert first.source == second.source
    assert first.execution_plan == second.execution_plan
    assert first.execution_plan.to_dict() == second.execution_plan.to_dict()
    assert first.execution_plan.source_sha256 == hashlib.sha256(
        first.source.encode("utf-8")
    ).hexdigest()

    # ``to_dict`` is an artifact boundary, so it must remain JSON serializable.
    assert json.loads(json.dumps(first.execution_plan.to_dict(), sort_keys=True))[
        "source_sha256"
    ] == first.execution_plan.source_sha256


def test_execution_plan_lists_ordered_calls_dependencies_and_only_used_tools():
    prepared = prepare(SOURCE_A, TOOL_NAMES, TOOL_SIGNERS)
    plan = prepared.execution_plan

    assert plan.allowed_tools == frozenset({"read_item", "publish_item"})
    assert [step.key for step in plan.steps] == ["read_item#0", "publish_item#0"]
    assert [step.tool for step in plan.steps] == ["read_item", "publish_item"]
    assert plan.steps[0].depends_on == ()
    assert plan.steps[1].depends_on == ("read_item",)
    assert plan.steps[1].depends_on_steps == ("read_item#0",)
    assert plan.to_dict()["allowed_tools"] == ["publish_item", "read_item"]


def test_execution_plan_tracks_dependencies_between_calls_to_the_same_tool():
    prepared = prepare(
        """\
def run():
    initial_item = read_item("item-1")
    related_item = read_item(initial_item.owner)
    publish_item(related_item.owner, "ready")
""",
        TOOL_NAMES,
        TOOL_SIGNERS,
    )

    assert prepared.execution_plan.steps[1].depends_on_steps == ("read_item#0",)
    assert prepared.execution_plan.steps[2].depends_on_steps == (
        "read_item#0",
        "read_item#1",
    )


class _Env:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []


def _tool(name: str, params: list[str], returns: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        params=params,
        signer="tiny",
        doc=ToolDoc(
            name=name,
            description=name,
            parameters=[
                {"name": param, "type": "string", "desc": param}
                for param in params
            ],
            returns=returns,
        ),
    )


_GATEWAY_TOOLS = {
    "echo": _tool("echo", ["value"], "object {value: string}"),
}


def _gateway_suite() -> SuiteSpec:
    env = _Env()

    def runner(tool: str, kwargs: dict[str, Any]) -> Any:
        env.calls.append((tool, kwargs))
        return {"value": kwargs["value"]}

    return SuiteSpec(
        name="tiny",
        tools=_GATEWAY_TOOLS,
        make_env=lambda: env,
        runner_factory=lambda _env: runner,
        tasks=[],
    )


def _loader(name: str) -> SuiteSpec:
    if name != "tiny":
        raise ValueError(name)
    return _gateway_suite()


class _StaticPlanner:
    def generate(self, prompt, suite_loader):
        return PlanDraft(
            suite_name="tiny",
            code='def run():\n    echo("hello")\n',
            reason="offline fixture",
        )


def test_gateway_exposes_the_compiler_derived_execution_plan():
    gateway = Gateway(_loader)
    assert gateway.current_execution_plan() is None

    result = gateway.submit_user_prompt_with_planner(
        "Echo hello.", _StaticPlanner(), generated_code_on_success=True
    )

    assert result.accepted
    actual = gateway.current_execution_plan()
    expected = prepare(
        'def run():\n    echo("hello")\n',
        {"echo"},
        {"echo": "tiny"},
    ).execution_plan
    assert actual == expected.to_dict()
    assert actual["allowed_tools"] == ["echo"]
