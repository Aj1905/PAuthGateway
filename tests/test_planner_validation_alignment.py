"""Repair must not discard code accepted by the downstream G2 normalizer."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from eval.planner_validation_audit import compare_validation
from gateway.planning.agentic_planner import generate_code_with_self_repair
from gateway.planning.prechecks import PrecheckPolicy
from pauth import prepare
from pauth.codegen import ToolDoc
from pauth.enforcer import Enforcer
from pauth.envelope import EnvelopeStore, KeyRing
from pauth.tool_executor import execute_generated_code


TOOLS = [ToolDoc(name="read_items", description="Read available items.", parameters=[],
                 returns="list of integers"),
         ToolDoc(name="report_count", description="Report item count.",
                 parameters=[{"name": "count", "type": "integer", "desc": "count"}],
                 returns="boolean")]


class Client:
    def __init__(self, code):
        self.calls = 0
        self.code = code
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.code))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )


@pytest.mark.parametrize("code", [
    "def run():\n    count = len(read_items())\n    report_count(count)\n",
    "def run():\n    items = read_items()\n    count = 0\n    count = len(items)\n    report_count(count)\n",
])
def test_normalizable_candidate_survives_without_regeneration_and_executes(code):
    names = {t.name for t in TOOLS}
    signers = {name: "fixture" for name in names}
    comparison = compare_validation(code, names, signers)
    assert comparison["historical_error"] is not None
    assert comparison["downstream_error"] is None
    client = Client(code)
    result = generate_code_with_self_repair(
        "Read the items and report their count", TOOLS,
        client=client, enable_judge=False, max_retries=2,
    )
    assert client.calls == result.attempts == 1
    assert result.failure_history == []
    prepared = prepare(result.code, names, signers)
    observed = []

    def execute(tool, kwargs):
        observed.append((tool, kwargs))
        return [10, 20] if tool == "read_items" else True

    report = execute_generated_code(
        prepared.source, Enforcer(prepared.rules, EnvelopeStore(KeyRing()), signers),
        {"read_items": [], "report_count": ["count"]}, execute,
    )
    assert report.crashed is None and not report.denied
    assert observed == [("read_items", {}), ("report_count", {"count": 2})]


def test_normalization_does_not_skip_forbidden_tool_precheck():
    client = Client("def run():\n    count = len(read_items())\n    report_count(count)\n")
    result = generate_code_with_self_repair(
        "Read the items", TOOLS, client=client, enable_judge=False, max_retries=1,
        precheck_policy=PrecheckPolicy(forbidden_tools=frozenset({"report_count"})),
    )
    assert client.calls == 2
    assert all(item.startswith("precheck:") for item in result.failure_history)
    assert "report_count" not in result.code


def test_unsupported_collection_tool_calls_still_trigger_grammar_repair():
    client = Client("def run():\n    items = read_items()\n    counts = [report_count(x) for x in items]\n")
    result = generate_code_with_self_repair(
        "Read items and report each", TOOLS, client=client,
        enable_judge=False, max_retries=1,
    )
    assert client.calls == 2
    assert any(item.startswith("grammar:") for item in result.failure_history)
