"""Auto strategy tests: recognizer fast path, LLM fallback."""

from __future__ import annotations

import types

import pytest

from pauth.suites.shopping import build_suite as build_shopping_suite

from gateway.planning.planner import (
    STRATEGY_AUTO,
    AutoPlanner,
    LLMFreeformPlanner,
    PlanDraft,
    PlanGenerationError,
    build_planner,
    normalize_strategy_name,
)

RECOGNIZED_PROMPT = (
    'If the product "Aurora Noise Cancelling Headphones" is in stock '
    "and costs less than $150.00, add 1 to my cart and pay the cart "
    'total to IBAN GB33BUKB20201555555555 with subject "Order payment" '
    "on 2024-06-11."
)
FREEFORM_PROMPT = "Please compare prices and buy whichever headphones are cheaper."


def _loader(name):
    if name != "shopping":
        raise ValueError(name)
    return build_shopping_suite()


class _RecordingFreeform:
    def __init__(self):
        self.calls = []

    def generate(self, prompt, suite_loader):
        self.calls.append(prompt)
        return PlanDraft(suite_name="shopping", code="def run():\n    pass\n", reason="llm")


def test_normalize_auto_and_hybrid_alias():
    assert normalize_strategy_name("auto") == STRATEGY_AUTO
    assert normalize_strategy_name("hybrid") == STRATEGY_AUTO


def test_recognized_prompt_uses_fast_path_without_llm():
    freeform = _RecordingFreeform()
    draft = AutoPlanner(freeform=freeform).generate(RECOGNIZED_PROMPT, _loader)
    assert draft.run_doc is not None  # recognizer output, not LLM
    assert freeform.calls == []


def test_unrecognized_prompt_falls_back_to_freeform():
    freeform = _RecordingFreeform()
    draft = AutoPlanner(freeform=freeform).generate(FREEFORM_PROMPT, _loader)
    assert draft.reason == "llm"
    assert freeform.calls == [FREEFORM_PROMPT]


def test_recognized_but_undeployed_suite_falls_back():
    # The banking regex matches, but this deployment only loads shopping.
    banking_prompt = (
        "If my bank balance is greater than $0.00, send $42.00 to IBAN "
        'DE89370400440532013000 with subject "rent" on 2024-01-01.'
    )
    freeform = _RecordingFreeform()
    draft = AutoPlanner(freeform=freeform).generate(banking_prompt, _loader)
    assert draft.reason == "llm"


def test_unrecognized_without_freeform_rejects_cleanly():
    with pytest.raises(PlanGenerationError, match="PAUTH_PLANNER_SUITE"):
        AutoPlanner(freeform=None).generate(FREEFORM_PROMPT, _loader)


def test_build_planner_auto_without_suite_has_no_fallback():
    planner = build_planner("auto", prompt=FREEFORM_PROMPT)
    assert isinstance(planner, AutoPlanner)
    assert planner.freeform is None


def test_build_planner_auto_with_suite_has_fallback():
    planner = build_planner("auto", prompt=FREEFORM_PROMPT, suite_name="shopping")
    assert isinstance(planner, AutoPlanner)
    assert planner.freeform is not None
    assert planner.freeform.suite_name == "shopping"


def test_claude_one_shot_uses_provider_aware_generator(monkeypatch):
    calls = []

    def fake_agentic(task, tools, **kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(code="def run():\n    pass\n")

    def fail_openai_only(*_args, **_kwargs):
        raise AssertionError("OpenAI-only generator must not receive a Claude model")

    monkeypatch.setattr(
        "gateway.planning.planner.generate_code_with_self_repair", fake_agentic
    )
    monkeypatch.setattr("gateway.planning.planner.generate_code", fail_openai_only)

    draft = LLMFreeformPlanner(
        suite_name="shopping",
        model="claude-fable-5",
        max_retries=0,
        enable_judge=True,
    ).generate("Inspect the cart.", _loader)

    assert draft.code == "def run():\n    pass\n"
    assert calls == [
        {
            "model": "claude-fable-5",
            "max_retries": 0,
            "cache_path": None,
            "enable_judge": False,
        }
    ]
