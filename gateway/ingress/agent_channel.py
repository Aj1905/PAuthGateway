"""Agent-facing channel.

Wire-shaped input/output for connecting an external agent (Claude Code,
custom LLM agents, etc.) to the gateway. Two message kinds:

* ``prompt`` -- the agent forwards the user's task prompt. Triggers plan
  generation. MUST be called exactly once per session before any tool call.
* ``tool_call`` -- the agent requests authorization to invoke a tool with
  concrete arguments. The gateway checks against the plan, executes the
  tool itself if permitted, records the observation envelope, and returns
  the result.

Trust shift recorded with this interface
----------------------------------------
The earlier converged design required the user prompt to bypass the agent
Routing it through the agent here re-introduces the
forwarding-integrity assumption: the agent MUST pass the user's prompt
unchanged. PAuth still defends against off-plan tool calls in the same
way; what we are *adding* to the trust set is "the agent does not silently
edit the user's prompt during the forward step". For Claude Code-style
integration this is unavoidable, but the assumption deserves to be
explicit.

Wire format
-----------
The dataclasses are JSON-serialisable. Use ``message_from_dict`` and
``response.to_dict()`` for HTTP/MCP transport. The Python types are
included so in-process callers (tests, demos) don't have to round-trip
through JSON.
"""

from __future__ import annotations

import dataclasses
import os
from typing import Any, Callable, Literal, Union

from pauth.suites.base import SuiteSpec

from gateway.runtime.audit import AuditLog
from gateway.runtime.gateway import CallResult, Gateway, SubmissionResult
from gateway.planning.planner import (
    STRATEGY_AUTO,
    STRATEGY_DETERMINISTIC,
    STRATEGY_LLM_FREEFORM,
    PlanGenerationError,
    build_planner,
    normalize_strategy_name,
)


# --------------------------------------------------------------------------
# Wire-shaped messages and responses (JSON-friendly).
# --------------------------------------------------------------------------

@dataclasses.dataclass
class PromptMessage:
    """Agent forwards the user's task prompt to the gateway."""

    kind: Literal["prompt"] = "prompt"
    prompt: str = ""
    # Planner strategy. If omitted, AgentChannel reads PAUTH_PLANNER_STRATEGY.
    # ``use_freeform`` remains as a backwards-compatible alias for
    # strategy="llm-freeform".
    strategy: str | None = None
    use_freeform: bool = False
    suite_name: str | None = None
    model: str = "gpt-4.1"
    max_retries: int = 3
    cache_dir: str | None = None
    enable_judge: bool = True
    judge_model: str | None = None


@dataclasses.dataclass
class ToolCallMessage:
    """Agent asks the gateway to authorize and execute a tool call.

    Either ``args`` (positional, schema-order) or ``kwargs`` (named) may
    be supplied. If both are present, ``kwargs`` wins. Claude Code's
    PreToolUse hook supplies ``tool_input`` as a dict, so the channel
    resolves dicts to schema order at the gateway boundary.
    """

    kind: Literal["tool_call"] = "tool_call"
    tool: str = ""
    args: list[Any] = dataclasses.field(default_factory=list)
    kwargs: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class PromptResponse:
    """Outcome of plan generation."""

    kind: Literal["prompt_result"] = "prompt_result"
    accepted: bool = False
    reason: str = ""
    rule_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class ToolCallResponse:
    """Outcome of a tool-call request."""

    kind: Literal["tool_call_result"] = "tool_call_result"
    permit: bool = False
    reason: str = ""
    return_value: Any | None = None
    reauthorization_required: bool = False
    authorization_permit: bool = False
    execution_status: str = "not_dispatched"

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class ErrorResponse:
    """Malformed message or protocol error."""

    kind: Literal["error"] = "error"
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# Runtime type aliases: use typing.Union so these evaluate on Python 3.9+
# (PEP 604 ``X | Y`` at runtime needs 3.10+; ``from __future__`` only defers
# annotations, not this module-level assignment).
AgentMessage = Union[PromptMessage, ToolCallMessage]
AgentResponse = Union[PromptResponse, ToolCallResponse, ErrorResponse]


def message_from_dict(payload: dict[str, Any]) -> AgentMessage | None:
    """Reconstruct a typed message from a JSON-decoded dict.

    Returns ``None`` when the payload does not match either message kind;
    callers should reply with :class:`ErrorResponse`.
    """
    kind = payload.get("kind")
    if kind == "prompt":
        return PromptMessage(
            prompt=str(payload.get("prompt", "")),
            strategy=payload.get("strategy"),
            use_freeform=_payload_bool(payload.get("use_freeform", False)),
            suite_name=payload.get("suite_name"),
            model=str(payload.get("model", "gpt-4.1")),
            max_retries=_payload_int(payload.get("max_retries", 3), 3),
            # cache_dir is a deployment setting, NOT wire-controllable: it is a
            # filesystem path that generated code is written to (mkdir + write),
            # so accepting it from the request body is an arbitrary-directory
            # write. Take it only from PAUTH_PLANNER_CACHE_DIR (see _handle_prompt).
            cache_dir=None,
            enable_judge=_payload_bool(payload.get("enable_judge", True)),
            judge_model=payload.get("judge_model"),
        )
    if kind == "tool_call":
        return ToolCallMessage(
            tool=str(payload.get("tool", "")),
            args=list(payload.get("args", [])),
            kwargs=dict(payload.get("kwargs", {})),
        )
    return None


# --------------------------------------------------------------------------
# Channel
# --------------------------------------------------------------------------

class AgentChannel:
    """One-session agent-facing wrapper around :class:`Gateway`.

    A single channel holds one task lifecycle:

    1. Agent sends a :class:`PromptMessage`. Plan is generated (once).
    2. Agent sends :class:`ToolCallMessage` repeatedly. Each is checked
       against the plan and either permitted (executed by the gateway,
       result returned to the agent) or rejected.

    Protocol invariants enforced here:

    * A prompt MUST arrive before any tool_call. Tool calls before a prompt
      receive an :class:`ErrorResponse` ("session has no plan").
    * A second prompt on the same channel is rejected. To start a new task
      the agent must instantiate a fresh channel. This preserves the
      "plan once" invariant and prevents an injection-victim agent from
      silently re-planning mid-task.
    """

    def __init__(
        self,
        suite_loader: Callable[[str], SuiteSpec],
        *,
        audit_log: "AuditLog | None" = None,
        restored_execution_state: dict[str, Any] | None = None,
        execution_state_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._gateway = Gateway(
            suite_loader,
            audit_log=audit_log,
            restored_execution_state=restored_execution_state,
            execution_state_sink=execution_state_sink,
        )
        self._prompt_received = False

    def status(self) -> dict[str, Any]:
        """Value-free session status for health checks (no operand values)."""
        return {"prompt_received": self._prompt_received, **self._gateway.status()}

    def execution_state(self) -> dict[str, Any] | None:
        """Operator-only durable attempt snapshot; never returned to the agent."""
        return self._gateway.current_execution_state()

    # ------------------------------------------------------------------
    # Primary entry: receive a typed message, return a typed response.
    # ------------------------------------------------------------------
    def receive(self, message: AgentMessage) -> AgentResponse:
        if isinstance(message, PromptMessage):
            return self._handle_prompt(message)
        if isinstance(message, ToolCallMessage):
            return self._handle_tool_call(message)
        return ErrorResponse(error=f"unsupported message kind: {type(message).__name__}")

    # JSON wire form -- handy for HTTP / MCP wrappers.
    def receive_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        msg = message_from_dict(payload)
        if msg is None:
            return ErrorResponse(error=f"unknown message kind: {payload.get('kind')!r}").to_dict()
        return self.receive(msg).to_dict()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _handle_prompt(self, message: PromptMessage) -> PromptResponse | ErrorResponse:
        if self._prompt_received:
            return ErrorResponse(
                error=(
                    "prompt already submitted on this channel; instantiate a "
                    "new channel for a new task (single-prompt invariant)"
                )
            )
        self._prompt_received = True

        try:
            strategy = _resolve_strategy(message)
            suite_name = message.suite_name or os.environ.get("PAUTH_PLANNER_SUITE")
            model = _env_or_message("PAUTH_PLANNER_MODEL", message.model)
            max_retries = _env_int("PAUTH_PLANNER_MAX_RETRIES", message.max_retries)
            cache_dir = message.cache_dir or os.environ.get("PAUTH_PLANNER_CACHE_DIR")
            enable_judge = _env_bool("PAUTH_PLANNER_ENABLE_JUDGE", message.enable_judge)
            judge_model = message.judge_model or os.environ.get("PAUTH_PLANNER_JUDGE_MODEL")
            canonical = normalize_strategy_name(strategy)
            planner = build_planner(
                canonical,
                prompt=message.prompt,
                suite_name=suite_name,
                model=model,
                max_retries=max_retries,
                cache_dir=cache_dir,
                enable_judge=enable_judge,
                judge_model=judge_model,
            )
        except PlanGenerationError as exc:
            return PromptResponse(accepted=False, reason=str(exc), rule_count=0)
        sub = self._gateway.submit_user_prompt_with_planner(
            message.prompt,
            planner,
            generated_code_on_success=canonical != STRATEGY_DETERMINISTIC,
        )

        return PromptResponse(
            accepted=sub.accepted,
            reason=sub.reason,
            rule_count=sub.rule_count,
        )

    def _handle_tool_call(self, message: ToolCallMessage) -> ToolCallResponse | ErrorResponse:
        if not self._prompt_received:
            return ErrorResponse(
                error="no prompt has been submitted on this channel; send a PromptMessage first"
            )
        # Resolve kwargs -> positional schema order if needed. The session's
        # tool_params is populated only after a successful prompt; if the
        # tool isn't known we still hand the call to ``handle_tool_call`` so
        # the gateway can produce the canonical "no rule exists" denial.
        args = list(message.args)
        if message.kwargs:
            params = self._gateway._session.tool_params.get(message.tool) if self._gateway._session else None  # noqa: SLF001
            if params:
                args = [message.kwargs.get(p) for p in params]
            else:
                # Fall back to ordered values; gateway will reject because
                # arity/operands won't match any rule.
                args = list(message.kwargs.values())
        result: CallResult = self._gateway.handle_tool_call(message.tool, args)
        # ``CallResult.return_value`` may be a non-JSON-serialisable pydantic
        # / dataclass object; serialise defensively for the wire payload.
        rv = _to_wire(result.return_value)
        # Surface the VALUE-FREE ``agent_reason`` on denials, never the internal
        # ``reason`` (which may quote an operand value). This is the channel
        # that re-enters the agent's model context.
        wire_reason = result.agent_reason if result.agent_reason is not None else result.reason
        return ToolCallResponse(
            permit=result.permit,
            reason=wire_reason,
            return_value=rv,
            reauthorization_required=result.reauthorization_required,
            authorization_permit=result.authorization_permit,
            execution_status=result.execution_status.value,
        )


def _to_wire(value: Any) -> Any:
    """Best-effort JSON-friendly projection of an arbitrary tool return."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "model_dump"):
        try:
            return _to_wire(value.model_dump())
        except Exception:  # noqa: BLE001
            pass
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _to_wire(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): _to_wire(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_wire(v) for v in value]
    return repr(value)


def _payload_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _payload_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _resolve_strategy(message: PromptMessage) -> str:
    if message.strategy:
        return message.strategy
    if message.use_freeform:
        return STRATEGY_LLM_FREEFORM
    # Default is the main-ingress strategy: recognizer fast
    # path with free-form fallback. Without PAUTH_PLANNER_SUITE the fallback
    # is absent, so the accepted set equals the old deterministic default.
    return os.environ.get("PAUTH_PLANNER_STRATEGY", STRATEGY_AUTO)


def _env_or_message(name: str, value: str) -> str:
    return os.environ.get(name, value)


def _env_int(name: str, value: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return value
    try:
        return int(raw)
    except ValueError as exc:
        raise PlanGenerationError(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(name: str, value: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return value
    return raw.strip().lower() in {"1", "true", "yes", "on"}
