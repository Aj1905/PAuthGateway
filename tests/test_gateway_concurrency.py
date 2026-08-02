"""In-process concurrency guarantees at the Gateway session boundary."""

from __future__ import annotations

import threading
from typing import Any, Callable

from pauth.codegen import ToolDoc
from pauth.suites.base import SuiteSpec, ToolSpec

from gateway.planning.planner import PlanDraft
from gateway.runtime.gateway import CallResult, Gateway


_PLAN = "def run():\n    send(5)\n"


class _StubPlanner:
    def generate(
        self,
        prompt: str,
        suite_loader: Callable[[str], SuiteSpec],
    ) -> PlanDraft:
        return PlanDraft(suite_name="concurrency", code=_PLAN, reason="stub")


def _gateway(tool_executor: Callable[[str, dict[str, Any]], Any]) -> Gateway:
    tool = ToolSpec(
        name="send",
        params=["amount"],
        signer="sink",
        doc=ToolDoc(
            name="send",
            description="Send an amount.",
            parameters=[{"name": "amount", "type": "number", "desc": "amount"}],
            returns="object {ok: boolean}",
        ),
    )
    suite = SuiteSpec(
        name="concurrency",
        tools={"send": tool},
        make_env=object,
        tool_executor_factory=lambda _env: tool_executor,
        tasks=[],
    )

    def load(name: str) -> SuiteSpec:
        if name != "concurrency":
            raise ValueError(name)
        return suite

    gateway = Gateway(load)
    submission = gateway.submit_user_prompt_with_planner("send 5", _StubPlanner())
    assert submission.accepted
    return gateway


def _join(thread: threading.Thread) -> None:
    thread.join(timeout=5)
    assert not thread.is_alive(), "worker did not complete"


def test_same_gateway_concurrent_replay_executes_tool_executor_once() -> None:
    tool_executor_entered = threading.Event()
    duplicate_tool_executor_entered = threading.Event()
    release_tool_executor = threading.Event()
    second_started = threading.Event()
    executions: list[dict[str, Any]] = []
    results: list[CallResult] = []
    executions_lock = threading.Lock()

    def tool_executor(tool: str, kwargs: dict[str, Any]) -> dict[str, bool]:
        with executions_lock:
            executions.append({"tool": tool, **kwargs})
            if len(executions) == 2:
                duplicate_tool_executor_entered.set()
        tool_executor_entered.set()
        assert release_tool_executor.wait(timeout=5), "test did not release tool_executor"
        return {"ok": True}

    gateway = _gateway(tool_executor)

    first = threading.Thread(
        target=lambda: results.append(gateway.handle_tool_call("send", [5]))
    )

    def invoke_second() -> None:
        second_started.set()
        results.append(gateway.handle_tool_call("send", [5]))

    second = threading.Thread(target=invoke_second)
    first.start()
    assert tool_executor_entered.wait(timeout=5), "first call did not reach tool_executor"
    second.start()
    assert second_started.wait(timeout=5), "second call did not start"
    try:
        assert not duplicate_tool_executor_entered.wait(timeout=1), (
            "concurrent replay reached tool_executor before the first call was recorded"
        )
    finally:
        release_tool_executor.set()

    _join(first)
    _join(second)

    assert executions == [{"tool": "send", "amount": 5}]
    assert sum(result.permit for result in results) == 1
    denied = next(result for result in results if not result.permit)
    assert "replay" in denied.reason


def test_different_gateways_execute_concurrently() -> None:
    tool_executors_meet = threading.Barrier(2)
    results: list[CallResult] = []

    def tool_executor(_tool: str, _kwargs: dict[str, Any]) -> dict[str, bool]:
        tool_executors_meet.wait(timeout=5)
        return {"ok": True}

    first_gateway = _gateway(tool_executor)
    second_gateway = _gateway(tool_executor)
    first = threading.Thread(
        target=lambda: results.append(first_gateway.handle_tool_call("send", [5]))
    )
    second = threading.Thread(
        target=lambda: results.append(second_gateway.handle_tool_call("send", [5]))
    )

    first.start()
    second.start()
    _join(first)
    _join(second)

    assert len(results) == 2
    assert all(result.permit for result in results)
