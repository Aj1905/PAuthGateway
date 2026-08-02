"""Offline tests for the P3/P4/P5 planner strategies.

P3 interactive-structuring, P4 specialized-codegen, P5 formal-semantic.
All LLM calls are faked; no network, no keys.
"""

from __future__ import annotations

import types

import pytest

from gateway.planning.formal_semantic import FormalSemanticPlanner, translate_formal_task
from gateway.planning.interactive_structuring import InteractiveStructuringPlanner
from gateway.planning.planner import (
    PlanGenerationError,
    build_planner,
    normalize_strategy_name,
)
from gateway.planning.specialized_codegen import SpecializedCodegenPlanner
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
    """Return canned outputs in order; repeat the last one when exhausted."""

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


def _tool(name: str, params: list[str]) -> ToolSpec:
    return ToolSpec(
        name=name,
        params=params,
        signer="tiny",
        doc=ToolDoc(
            name=name,
            description=name,
            parameters=[{"name": p, "type": "string", "desc": p} for p in params],
            returns="object",
        ),
    )


def _suite() -> SuiteSpec:
    return SuiteSpec(
        name="tiny",
        tools={
            "notify": _tool("notify", ["value"]),
            "read_message": _tool("read_message", []),
            "send_money": _tool("send_money", ["iban", "amount", "subject", "date"]),
        },
        make_env=lambda: None,
        tool_executor_factory=lambda _env: (lambda _tool, _kwargs: {"ok": True}),
        tasks=[],
    )


def _loader(name: str) -> SuiteSpec:
    if name != "tiny":
        raise ValueError(name)
    return _suite()


_VALID_CODE = 'def run():\n    notify("hi")\n'
_INVALID_CODE = 'def run():\n    while True:\n        notify("hi")\n'


# ---------------------------------------------------------------- P4 ------


def test_p4_accepts_first_valid_candidate():
    planner = SpecializedCodegenPlanner(
        suite_name="tiny", client=_FakeClient([_VALID_CODE])
    )
    draft = planner.generate("notify hi", _loader)
    assert 'notify("hi")' in draft.code
    assert draft.planner_metadata["attempts"] == 1
    assert draft.planner_metadata["failure_history"] == []


def test_p4_feeds_rejection_back_and_recovers():
    client = _FakeClient([_INVALID_CODE, _VALID_CODE])
    planner = SpecializedCodegenPlanner(suite_name="tiny", client=client)
    draft = planner.generate("notify hi", _loader)
    assert draft.planner_metadata["attempts"] == 2
    assert any("pipeline:" in f for f in draft.planner_metadata["failure_history"])
    # The repair turn must carry the rejection back to the model.
    repair_msgs = client.calls[1]["messages"]
    assert any("REJECTED" in m["content"] for m in repair_msgs if m["role"] == "user")


def test_p4_exhausted_budget_raises_not_sentinel():
    planner = SpecializedCodegenPlanner(
        suite_name="tiny", client=_FakeClient([_INVALID_CODE]), max_retries=1
    )
    with pytest.raises(PlanGenerationError, match="retry budget"):
        planner.generate("notify hi", _loader)


def test_p4_registration_and_suite_requirement():
    assert normalize_strategy_name("specialized") == "specialized-codegen"
    with pytest.raises(PlanGenerationError, match="requires suite_name"):
        build_planner("specialized-codegen", prompt="x")
    planner = build_planner("specialized-codegen", prompt="x", suite_name="tiny")
    assert isinstance(planner, SpecializedCodegenPlanner)


# ---------------------------------------------------------------- P5 ------


def test_p5_parses_bind_cond_and_call():
    text = (
        'm = call read_message; '
        'if m.amount <= 100 then call send_money with m.iban, m.amount, '
        '"Order", "2024-01-01"'
    )
    code, n = translate_formal_task(text, _suite())
    assert n == 2
    assert "m = read_message()" in code
    assert "if m.amount <= 100:" in code
    assert "send_money(m.iban, m.amount, 'Order', '2024-01-01')" in code
    suite = _suite()
    prepare(code, suite.tool_names(), suite.tool_signer())  # must compile


def test_p5_planner_draft_and_metadata():
    draft = FormalSemanticPlanner(suite_name="tiny").generate(
        'call notify with "hi"', _loader
    )
    assert "notify('hi')" in draft.code
    assert draft.planner_metadata["language"] == "fsl-1"


@pytest.mark.parametrize(
    "bad,fragment",
    [
        ("call unknown_tool", "unknown tool"),
        ('call notify with "a", "b"', "takes 1 argument"),
        ('call send_money with x.iban, 1, "s", "d"', "does not resolve"),
        ("please notify everyone", "outside fsl-1"),
        ("m = call read_message; if m.amount then call notify with \"x\"", "outside fsl-1"),
    ],
)
def test_p5_rejects_instead_of_completing(bad, fragment):
    with pytest.raises(PlanGenerationError, match=fragment):
        FormalSemanticPlanner(suite_name="tiny").generate(bad, _loader)


def test_p5_registration_and_suite_requirement():
    assert normalize_strategy_name("formal") == "formal-semantic"
    with pytest.raises(PlanGenerationError, match="requires suite_name"):
        build_planner("formal-semantic", prompt="x")
    assert isinstance(
        build_planner("formal-semantic", prompt="x", suite_name="tiny"),
        FormalSemanticPlanner,
    )


# ---------------------------------------------------------------- P3 ------


def test_p3_complete_prompt_skips_questions():
    client = _FakeClient(
        ['{"structured_prompt": "Notify with the literal text hi."}', _VALID_CODE]
    )
    planner = InteractiveStructuringPlanner(
        suite_name="tiny", client=client, enable_judge=False
    )
    draft = planner.generate("notify hi", _loader)
    assert 'notify("hi")' in draft.code
    meta = draft.planner_metadata
    assert meta["questions"] == [] and meta["answers"] == {}
    assert meta["structured_prompt"].startswith("Notify")
    assert meta["raw_prompt"] == "notify hi"


def test_p3_question_round_feeds_answers_into_structuring():
    asked: list[list[str]] = []

    def clarifier(questions):
        asked.append(list(questions))
        return {questions[0]: "hi"}

    client = _FakeClient(
        [
            '{"questions": ["What text should the notification carry?"]}',
            '{"structured_prompt": "Notify with the literal text hi."}',
            _VALID_CODE,
        ]
    )
    planner = InteractiveStructuringPlanner(
        suite_name="tiny", client=client, enable_judge=False, clarifier=clarifier
    )
    draft = planner.generate("notify something", _loader)
    assert asked == [["What text should the notification carry?"]]
    meta = draft.planner_metadata
    assert meta["questions"] == ["What text should the notification carry?"]
    assert meta["answers"] == {"What text should the notification carry?": "hi"}
    # The answer round must include the Q/A pairs in the model conversation.
    round2 = client.calls[1]["messages"]
    assert any("A: hi" in m["content"] for m in round2 if m["role"] == "user")


def test_p3_questions_without_clarifier_reject_cleanly():
    client = _FakeClient(['{"questions": ["Which IBAN?"]}'])
    planner = InteractiveStructuringPlanner(
        suite_name="tiny", client=client, enable_judge=False
    )
    with pytest.raises(PlanGenerationError, match="clarifier"):
        planner.generate("send money to my usual account", _loader)


def test_p3_nonconverging_clarification_rejects():
    client = _FakeClient(
        [
            '{"questions": ["Which IBAN?"]}',
            '{"questions": ["Which currency?"]}',
        ]
    )
    planner = InteractiveStructuringPlanner(
        suite_name="tiny",
        client=client,
        enable_judge=False,
        clarifier=lambda qs: {qs[0]: "GB33BUKB20201555555555"},
    )
    with pytest.raises(PlanGenerationError, match="did not converge"):
        planner.generate("send money", _loader)


def test_p3_registration_and_suite_requirement():
    assert normalize_strategy_name("interactive") == "interactive-structuring"
    with pytest.raises(PlanGenerationError, match="requires suite_name"):
        build_planner("interactive-structuring", prompt="x")
    planner = build_planner(
        "interactive-structuring", prompt="x", suite_name="tiny"
    )
    assert isinstance(planner, InteractiveStructuringPlanner)
    assert planner.clarifier is None
