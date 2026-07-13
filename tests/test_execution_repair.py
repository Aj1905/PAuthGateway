"""Agentic runtime-repair stage: a grammar-valid candidate that CRASHES when
executed is fed back for repair; if it still crashes after the retry budget it
is replaced with the reject sentinel (never counted as an accepted plan).

The LLM and the dry-run executor are both stubbed so the test is deterministic
and offline -- it exercises only the loop's control flow, not a real model."""

from __future__ import annotations

import types

import pytest

from gateway.planning.agentic_a1 import generate_code_with_self_repair
from pauth.codegen import ToolDoc

TOOLS = [
    ToolDoc(name="get_items", description="list items", parameters=[], returns="list"),
    ToolDoc(name="use_item", description="use one item",
            parameters=[{"name": "item", "type": "any"}], returns="none"),
]

# Both are grammar-valid; only the first "crashes" per the stub executor below.
CRASHING = "def run():\n    x = get_items()\n    use_item(x)\n"
CLEAN = "def run():\n    x = get_items()\n    pass\n"


class _FakeResp:
    def __init__(self, content: str) -> None:
        self.choices = [types.SimpleNamespace(
            message=types.SimpleNamespace(content=content))]
        self.usage = types.SimpleNamespace(prompt_tokens=1, completion_tokens=1)


class _FakeClient:
    """Returns queued completions in order; repeats the last one if exhausted."""

    def __init__(self, outputs: list[str]) -> None:
        self._outputs = outputs
        self.calls = 0
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create))

    def _create(self, **_kw) -> _FakeResp:
        out = self._outputs[min(self.calls, len(self._outputs) - 1)]
        self.calls += 1
        return _FakeResp(out)


# Executor stub: any code that still calls use_item "crashes"; CLEAN does not.
def _stub_exec(code: str) -> str | None:
    return "TypeError: boom" if "use_item" in code else None


def test_runtime_crash_is_repaired():
    client = _FakeClient([CRASHING, CLEAN])
    res = generate_code_with_self_repair(
        "list the items", TOOLS, model="gpt-4.1", max_retries=2,
        cache_path=None, client=client, enable_judge=False, executor=_stub_exec,
    )
    assert res.code.strip() == CLEAN.strip()
    assert res.attempts == 2
    assert any(h.startswith("runtime:") for h in res.failure_history)


def test_persistent_crash_becomes_reject_sentinel():
    # The model never fixes it -> after the retry budget, the crashing plan is
    # replaced by `def run(): pass`, so it is rejected, not accepted.
    client = _FakeClient([CRASHING])
    res = generate_code_with_self_repair(
        "list the items", TOOLS, model="gpt-4.1", max_retries=2,
        cache_path=None, client=client, enable_judge=False, executor=_stub_exec,
    )
    assert res.code.strip() == "def run():\n    pass".strip()
    # one initial + max_retries repair rounds all failed the runtime stage
    assert sum(h.startswith("runtime:") for h in res.failure_history) == 3


def test_no_executor_preserves_old_behavior():
    # Without an executor the runtime stage is skipped: crashing-but-valid code
    # is returned unchanged (the loop cannot see the crash).
    client = _FakeClient([CRASHING])
    res = generate_code_with_self_repair(
        "list the items", TOOLS, model="gpt-4.1", max_retries=2,
        cache_path=None, client=client, enable_judge=False, executor=None,
    )
    assert res.code.strip() == CRASHING.strip()
    assert not any(h.startswith("runtime:") for h in res.failure_history)
