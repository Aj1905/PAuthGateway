"""Offline tests for the deletion-only Sufficiency–Tightness planner."""

from __future__ import annotations

import ast
import json
import types

import pytest

from gateway.planning.planner import (
    STRATEGY_SUFFICIENCY_TIGHTNESS,
    PlanGenerationError,
    SufficiencyTightnessPlanner,
    build_planner,
    normalize_strategy_name,
)
from pauth import prepare
from pauth.codegen import ToolDoc
from pauth.suites.base import SuiteSpec, ToolSpec


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [
            types.SimpleNamespace(message=types.SimpleNamespace(content=content))
        ]
        self.usage = types.SimpleNamespace(prompt_tokens=1, completion_tokens=1)


class _FakeClient:
    """Return one Phase 1 program followed by one Phase 2 JSON decision."""

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.calls: list[dict] = []
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create)
        )

    def _create(self, **kwargs) -> _FakeResponse:
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self.outputs) - 1)
        return _FakeResponse(self.outputs[index])


def _tool(name: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        params=["value"],
        signer="tiny",
        doc=ToolDoc(
            name=name,
            description=name,
            parameters=[{"name": "value", "type": "string", "desc": "value"}],
            returns="object",
        ),
    )


_TOOLS = {
    "notify": _tool("notify"),
    "archive": _tool("archive"),
}


def _suite() -> SuiteSpec:
    return SuiteSpec(
        name="tiny",
        tools=_TOOLS,
        make_env=lambda: None,
        tool_executor_factory=lambda _env: (lambda _tool, _kwargs: {"ok": True}),
        tasks=[],
    )


def _loader(name: str) -> SuiteSpec:
    if name != "tiny":
        raise ValueError(name)
    return _suite()


def _calls(code: str) -> list[tuple[str, str]]:
    func = ast.parse(code).body[0]
    assert isinstance(func, ast.FunctionDef)
    calls: list[tuple[str, str]] = []
    for statement in func.body:
        if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
            continue
        call = statement.value
        assert isinstance(call.func, ast.Name)
        assert len(call.args) == 1 and isinstance(call.args[0], ast.Constant)
        calls.append((call.func.id, call.args[0].value))
    return calls


def test_strategy_is_registered_without_changing_existing_strategy_defaults():
    assert (
        normalize_strategy_name("sufficiency-tightness")
        == STRATEGY_SUFFICIENCY_TIGHTNESS
    )
    planner = build_planner(
        "sufficiency-tightness",
        prompt="Notify only the second recipient.",
        suite_name="tiny",
        model="fake-model",
    )
    assert isinstance(planner, SufficiencyTightnessPlanner)


def test_phase_two_can_only_delete_phase_one_action_ids():
    phase_one = """\
def run():
    notify("first")
    notify("second")
    archive("old")
"""
    phase_two = json.dumps(
        {
            "keep_action_ids": ["notify#1"],
            "drop_reasons": {
                "notify#0": "not requested",
                "archive#0": "not requested",
            },
        }
    )
    client = _FakeClient([phase_one, phase_two])
    planner = SufficiencyTightnessPlanner(
        suite_name="tiny",
        model="fake-model",
        client=client,
        max_retries=0,
        enable_judge=False,
    )

    draft = planner.generate("Notify only the second recipient.", _loader)

    assert len(client.calls) == 2
    assert _calls(draft.code) == [("notify", "second")]
    final_plan = prepare(
        draft.code, set(_TOOLS), {name: spec.signer for name, spec in _TOOLS.items()}
    ).execution_plan
    assert final_plan.allowed_tools == frozenset({"notify"})
    assert final_plan.allowed_tools <= prepare(
        phase_one, set(_TOOLS), {name: spec.signer for name, spec in _TOOLS.items()}
    ).execution_plan.allowed_tools


def test_phase_two_unknown_action_id_fails_closed_instead_of_adding_a_tool():
    phase_one = 'def run():\n    notify("only")\n'
    malicious_phase_two = json.dumps(
        {
            "keep_action_ids": ["notify#0", "wire_money#0"],
            "drop_reasons": {},
        }
    )
    client = _FakeClient([phase_one, malicious_phase_two])
    planner = SufficiencyTightnessPlanner(
        suite_name="tiny",
        model="fake-model",
        client=client,
        max_retries=0,
        enable_judge=False,
    )

    with pytest.raises(PlanGenerationError, match="wire_money#0|unknown action"):
        planner.generate("Notify only.", _loader)


def test_phase_two_reduces_the_compiler_normalized_source():
    # The compiler rewrites this call-as-argument into two statement-level
    # actions. The audit IDs and reducer must both operate on that same source.
    phase_one = 'def run():\n    notify(archive("old"))\n'
    phase_two = json.dumps(
        {
            "keep_action_ids": ["archive#0"],
            "drop_reasons": {"notify#0": "not requested"},
        }
    )
    planner = SufficiencyTightnessPlanner(
        suite_name="tiny",
        model="fake-model",
        client=_FakeClient([phase_one, phase_two]),
        max_retries=0,
        enable_judge=False,
    )

    draft = planner.generate("Archive old only.", _loader)

    assert _calls(draft.code) == []
    prepared = prepare(
        draft.code, set(_TOOLS), {name: spec.signer for name, spec in _TOOLS.items()}
    )
    assert prepared.execution_plan.allowed_tools == frozenset({"archive"})
