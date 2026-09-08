"""Gateway planning rules and targeted grammar-repair reminders (T16).

The self-repair Planner sends the paper's grammar prompt PLUS the gateway rules
by default, while the one-shot paper-reproduction prompt stays untouched. The
repair loop also gives a targeted reminder for the single-definition violation,
which used to fall through to the generic text. Offline: the LLM is stubbed."""

from __future__ import annotations

import types

from gateway.planning.agentic_planner import (
    GATEWAY_PLANNER_RULES,
    INVENTORY_NOTE,
    PLANNER_SYSTEM_PROMPT,
    _PRECHECK_REPAIR_INSTRUCTION,
    _rule_reminder,
    generate_code_with_self_repair,
)
from pauth.codegen import SYSTEM_PROMPT, ToolDoc
from pauth.grammar_validator import DSLRejectionError, parse_and_validate, validate_semantics

TOOLS = [
    ToolDoc(name="get_items", description="list items", parameters=[], returns="list"),
]


class _FakeResp:
    def __init__(self, content: str) -> None:
        self.choices = [types.SimpleNamespace(message=types.SimpleNamespace(content=content))]
        self.usage = types.SimpleNamespace(prompt_tokens=1, completion_tokens=1)


class _FakeClient:
    def __init__(self, outputs: list[str]) -> None:
        self._outputs, self.calls, self.request_kwargs = outputs, 0, []
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))

    def _create(self, **kwargs) -> _FakeResp:
        self.request_kwargs.append(kwargs)
        out = self._outputs[min(self.calls, len(self._outputs) - 1)]
        self.calls += 1
        return _FakeResp(out)


def test_planner_prompt_extends_paper_prompt_without_changing_it():
    assert PLANNER_SYSTEM_PROMPT.startswith(SYSTEM_PROMPT)
    assert GATEWAY_PLANNER_RULES in PLANNER_SYSTEM_PROMPT
    assert "GATEWAY PLANNING RULES" not in SYSTEM_PROMPT  # paper prompt untouched


def test_self_repair_planner_sends_gateway_rules_by_default():
    client = _FakeClient(["def run():\n    x = get_items()\n    pass\n"])
    generate_code_with_self_repair("list the items", TOOLS, model="gpt-4.1", max_retries=1,
                                   client=client, enable_judge=False)
    system = client.request_kwargs[0]["messages"][0]["content"]
    # p5: the gateway rules plus the inventory note (E2d) are the default.
    assert system == PLANNER_SYSTEM_PROMPT + "\n\n" + INVENTORY_NOTE


def test_explicit_system_prompt_still_overrides():
    client = _FakeClient(["def run():\n    x = get_items()\n    pass\n"])
    generate_code_with_self_repair("list the items", TOOLS, model="gpt-4.1", max_retries=1,
                                   client=client, enable_judge=False,
                                   initial_system_prompt=SYSTEM_PROMPT + "\n\nEXTRA")
    assert client.request_kwargs[0]["messages"][0]["content"].endswith("EXTRA")


def test_precheck_repair_prefers_lookup_over_sentinel():
    # The sentinel is offered only as the LAST option, after resolving via a tool.
    lookup = _PRECHECK_REPAIR_INSTRUCTION.index("RESOLVE the value at run time")
    sentinel = _PRECHECK_REPAIR_INSTRUCTION.index("def run():")
    assert lookup < sentinel
    assert "placeholder" in _PRECHECK_REPAIR_INSTRUCTION


def test_single_definition_violation_gets_targeted_reminder():
    code = "def run():\n    x = get_items()\n    y = 1\n    if len(x) > 0:\n        y = 2\n    if len(x) > 1:\n        y = 3\n"
    try:
        validate_semantics(parse_and_validate(code), {"get_items"})
    except DSLRejectionError as exc:
        reminder = _rule_reminder(str(exc))
    else:  # pragma: no cover - the validator must reject this shape
        raise AssertionError("expected a single-definition rejection")
    assert "exactly ONCE" in reminder
    assert "Review Appendix A" not in reminder  # not the generic fallback


def test_for_inside_if_reminder_shows_filter_then_loop_shape():
    reminder = _rule_reminder("for-loops may not appear inside an if body (rule 10)")
    assert "list comprehension" in reminder
    assert "selected = [" in reminder
