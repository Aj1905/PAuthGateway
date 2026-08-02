"""Crash-safe at-most-once dispatch for one Gateway session."""

from __future__ import annotations

import copy
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, Callable

from pauth.codegen import ToolDoc
from pauth.suites.base import SuiteSpec, ToolSpec

from gateway.planning.planner import PlanDraft
from gateway.runtime.gateway import ExecutionStatus, Gateway
from gateway.serving.http_server import restore_channel
from gateway.serving.session_store import SessionStore


_PLAN = "def run():\n    send(5)\n"


class _Planner:
    def generate(
        self,
        prompt: str,
        suite_loader: Callable[[str], SuiteSpec],
    ) -> PlanDraft:
        return PlanDraft(suite_name="durability", code=_PLAN, reason="stub")


def _build_gateway(
    tool_executor: Callable[[str, dict[str, Any]], Any],
    *,
    restored: dict[str, Any] | None = None,
    sink: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[Gateway, Any]:
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
        name="durability",
        tools={"send": tool},
        make_env=object,
        tool_executor_factory=lambda _env: tool_executor,
        tasks=[],
    )

    def loader(name: str) -> SuiteSpec:
        if name != "durability":
            raise ValueError(name)
        return suite

    gateway = Gateway(
        loader,
        restored_execution_state=restored,
        execution_state_sink=sink,
    )
    submission = gateway.submit_user_prompt_with_planner("send 5", _Planner())
    return gateway, submission


def _snapshot_sink(target: dict[str, Any]) -> Callable[[dict[str, Any]], None]:
    def persist(state: dict[str, Any]) -> None:
        target.clear()
        target.update(copy.deepcopy(state))

    return persist


def test_side_effect_then_tool_executor_exception_blocks_immediate_and_restart_retry() -> None:
    effects: list[int] = []
    durable: dict[str, Any] = {}

    def tool_executor(_tool: str, kwargs: dict[str, Any]) -> None:
        effects.append(kwargs["amount"])
        raise TimeoutError("response lost after commit")

    gateway, submission = _build_gateway(tool_executor, sink=_snapshot_sink(durable))
    assert submission.accepted

    first = gateway.handle_tool_call("send", [5])
    assert not first.permit
    assert first.authorization_permit
    assert first.execution_status == ExecutionStatus.INDETERMINATE
    assert "indeterminate tool outcome" in first.reason
    assert effects == [5]
    assert durable["calls"][0]["state"] == "indeterminate"

    immediate = gateway.handle_tool_call("send", [5])
    assert not immediate.permit
    assert effects == [5]

    restored, restored_submission = _build_gateway(
        tool_executor,
        restored=copy.deepcopy(durable),
        sink=_snapshot_sink(durable),
    )
    assert restored_submission.accepted
    after_restart = restored.handle_tool_call("send", [5])
    assert not after_restart.permit
    assert effects == [5]


def test_successful_call_remains_consumed_after_restart() -> None:
    effects: list[int] = []
    durable: dict[str, Any] = {}

    def tool_executor(_tool: str, kwargs: dict[str, Any]) -> dict[str, bool]:
        effects.append(kwargs["amount"])
        return {"ok": True}

    gateway, submission = _build_gateway(tool_executor, sink=_snapshot_sink(durable))
    assert submission.accepted
    result = gateway.handle_tool_call("send", [5])
    assert result.permit
    assert result.execution_status == ExecutionStatus.SUCCEEDED
    assert durable["calls"][0]["state"] == "succeeded"

    restored, restored_submission = _build_gateway(
        tool_executor,
        restored=copy.deepcopy(durable),
        sink=_snapshot_sink(durable),
    )
    assert restored_submission.accepted
    assert not restored.handle_tool_call("send", [5]).permit
    assert effects == [5]


def test_pre_dispatch_persistence_failure_never_calls_tool_executor() -> None:
    effects: list[int] = []

    def tool_executor(_tool: str, kwargs: dict[str, Any]) -> dict[str, bool]:
        effects.append(kwargs["amount"])
        return {"ok": True}

    def failing_sink(state: dict[str, Any]) -> None:
        if state["calls"]:
            raise OSError("disk unavailable")

    gateway, submission = _build_gateway(tool_executor, sink=failing_sink)
    assert submission.accepted
    result = gateway.handle_tool_call("send", [5])
    assert not result.permit
    assert result.execution_status == ExecutionStatus.NOT_DISPATCHED
    assert "could not be durably recorded" in result.reason
    assert result.agent_reason == (
        "Tool send cannot run because its execution state was not safely recorded."
    )
    assert gateway.audit_log()[-1].decision == "error"
    assert gateway.audit_log()[-1].reason_code == "execution_state_error"
    assert effects == []

    # The ambiguous persistence failure is fail-closed in this process too.
    assert not gateway.handle_tool_call("send", [5]).permit
    assert effects == []


def test_post_tool_executor_persistence_failure_restores_started_as_indeterminate() -> None:
    effects: list[int] = []
    durable: dict[str, Any] = {}

    def tool_executor(_tool: str, kwargs: dict[str, Any]) -> dict[str, bool]:
        effects.append(kwargs["amount"])
        return {"ok": True}

    def fail_completed_write(state: dict[str, Any]) -> None:
        if state["calls"] and state["calls"][0]["state"] == "succeeded":
            raise OSError("completion fsync failed")
        durable.clear()
        durable.update(copy.deepcopy(state))

    gateway, submission = _build_gateway(tool_executor, sink=fail_completed_write)
    assert submission.accepted
    result = gateway.handle_tool_call("send", [5])
    assert not result.permit
    assert result.execution_status == ExecutionStatus.INDETERMINATE
    assert effects == [5]
    assert durable["calls"][0]["state"] == "started"
    assert not gateway.handle_tool_call("send", [5]).permit

    restored, restored_submission = _build_gateway(
        tool_executor,
        restored=copy.deepcopy(durable),
        sink=_snapshot_sink(durable),
    )
    assert restored_submission.accepted
    assert durable["calls"][0]["state"] == "indeterminate"
    assert not restored.handle_tool_call("send", [5]).permit
    assert effects == [5]


def test_pending_confirmation_and_arity_denial_create_no_attempt() -> None:
    effects: list[int] = []
    durable: dict[str, Any] = {}

    def tool_executor(_tool: str, kwargs: dict[str, Any]) -> dict[str, bool]:
        effects.append(kwargs["amount"])
        return {"ok": True}

    gateway, submission = _build_gateway(tool_executor, sink=_snapshot_sink(durable))
    assert submission.accepted
    assert gateway._session is not None  # noqa: SLF001 -- white-box safety invariant
    gateway._session.gated_operands = {("send", 0)}  # noqa: SLF001

    pending = gateway.handle_tool_call("send", [5])
    assert not pending.permit
    assert pending.authorization_permit
    assert effects == []
    assert gateway.current_execution_state()["calls"] == []

    confirmation = gateway.pending_confirmations()[0]
    assert gateway.confirm(confirmation.confirmation_id, True)
    assert gateway.handle_tool_call("send", [5]).permit
    assert effects == [5]

    other, other_submission = _build_gateway(tool_executor)
    assert other_submission.accepted
    arity = other.handle_tool_call("send", [])
    assert not arity.permit
    assert other.current_execution_state()["calls"] == []


def test_plan_fingerprint_mismatch_rejects_restore() -> None:
    durable: dict[str, Any] = {}
    gateway, submission = _build_gateway(
        lambda _tool, _kwargs: {"ok": True},
        sink=_snapshot_sink(durable),
    )
    assert submission.accepted
    state = gateway.current_execution_state()
    assert state is not None
    state["plan_source_sha256"] = "0" * 64

    _restored, restored_submission = _build_gateway(
        lambda _tool, _kwargs: {"ok": True},
        restored=state,
    )
    assert not restored_submission.accepted
    assert "plan fingerprint mismatch" in restored_submission.reason


def test_indeterminate_outcome_is_audit_error_not_enforcer_denial() -> None:
    gateway, submission = _build_gateway(
        lambda _tool, _kwargs: (_ for _ in ()).throw(TimeoutError("lost"))
    )
    assert submission.accepted
    result = gateway.handle_tool_call("send", [5])
    assert not result.permit
    assert result.agent_reason == (
        "The outcome of tool send is unknown; automatic retry is blocked."
    )
    assert "lost" not in result.agent_reason
    event = gateway.audit_log()[-1]
    assert event.decision == "indeterminate"
    assert event.reason_code == "indeterminate_tool_outcome"


def test_process_exit_during_tool_executor_is_replay_blocked_after_real_restart(tmp_path) -> None:
    store_path = tmp_path / "sessions.json"
    effect_path = tmp_path / "effects.log"
    script = textwrap.dedent(
        """
        import os
        import sys

        from gateway.ingress.agent_channel import AgentChannel
        from gateway.serving.session_store import SessionStore
        from pauth.suites.shopping import build_suite

        store_path, effect_path = sys.argv[1:]
        suite = build_suite()
        base_factory = suite.tool_executor_factory

        def tool_executor_factory(env):
            base = base_factory(env)
            def tool_executor(tool, kwargs):
                if tool == "get_product_details":
                    fd = os.open(effect_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
                    try:
                        os.write(fd, b"effect\\n")
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                    os._exit(17)
                return base(tool, kwargs)
            return tool_executor

        suite.tool_executor_factory = tool_executor_factory
        def loader(name):
            if name != "shopping":
                raise ValueError(name)
            return suite

        prompt = (
            'If the product "Aurora Noise Cancelling Headphones" is in stock '
            'and costs less than $150.00, add 1 to my cart and pay the cart '
            'total to IBAN GB33BUKB20201555555555 with subject "Order payment" '
            'on 2024-06-11.'
        )
        store = SessionStore(store_path)
        channel = AgentChannel(
            loader,
            execution_state_sink=lambda state: store.update_execution_state("sid", state),
        )
        accepted = channel.receive_json({"kind": "prompt", "prompt": prompt})
        assert accepted["accepted"]
        store.record("sid", prompt, {}, execution_state=channel.execution_state())
        channel.receive_json({
            "kind": "tool_call",
            "tool": "get_product_details",
            "kwargs": {"name": "Aurora Noise Cancelling Headphones"},
        })
        raise AssertionError("tool_executor did not terminate the process")
        """
    )
    crashed = subprocess.run(
        [sys.executable, "-c", script, str(store_path), str(effect_path)],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
    )
    assert crashed.returncode == 17

    store = SessionStore(store_path)
    assert store.get("sid")["execution_state"]["calls"][0]["state"] == "started"

    from pauth.suites.shopping import build_suite

    def loader(name):
        if name != "shopping":
            raise ValueError(name)
        return build_suite()

    channel = restore_channel(loader, store, "sid")
    assert channel is not None
    replay = channel.receive_json({
        "kind": "tool_call",
        "tool": "get_product_details",
        "kwargs": {"name": "Aurora Noise Cancelling Headphones"},
    })
    assert not replay["permit"]
    assert effect_path.read_text().splitlines() == ["effect"]
    assert store.get("sid")["execution_state"]["calls"][0]["state"] == "indeterminate"
