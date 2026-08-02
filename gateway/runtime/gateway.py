"""Runtime gateway: plan once (from the user prompt), enforce per call.

Architecture (the converged design):

    User -----submit_user_prompt(prompt)----> Gateway
                                              |
                            deterministic NL -> run() -> rules
                                              |    (ONCE per task)
    Agent ----handle_tool_call(tool, args)--> Gateway
                                              |
                            check operator + operand against rules
                            gateway executes the tool itself
                            records a signed observation envelope

Hard invariants enforced by this module:

* Plan generation runs only inside ``submit_user_prompt``. ``handle_tool_call``
  never regenerates the plan, so the agent's request can never influence the
  defence boundary. The transcript's "input-path contamination" hole is closed
  structurally: the agent has no API surface that touches plan generation.

* The gateway is the sole observation authority. Every tool result is wrapped
  into an HMAC-signed envelope produced by the gateway's keyring and stored in
  the gateway's envelope store. Operand verification reads from that store, so
  an injection-victim agent reporting a fabricated intermediate value cannot
  bias subsequent operand checks.

* PAuth's default-deny is preserved: a call without a matching rule, or whose
  operands disagree with the stored observations, is rejected.

This is the PAuth runtime relocated from "each SaaS server enforces" to "one
local gateway enforces". The signature root moves from per-server keys to the
gateway's local key. The transcript labels this faithfully: a personal,
client-side, task-scoped firewall built on PAuth's algorithms.
"""

from __future__ import annotations

import copy
import dataclasses
import enum
import math
import struct
import threading
from pathlib import Path
from typing import Any, Callable

from pauth import ExecutionPlan, prepare
from pauth.enforcer import Enforcer
from pauth.envelope import EnvelopeStore, KeyRing
from pauth.evaluator import wrap
from pauth.suites.base import SuiteSpec

from gateway.planning.composite import (
    CompositePlan,
    CompositePlanError,
    GuardNotEvaluable,
    assignment_map,
    eval_guard,
    instantiate_fanout,
    validate_plan,
)
from gateway.planning.planner import (
    DeterministicRecognizerPlanner,
    LLMFreeformPlanner,
    PlanDraft,
    PlanGenerationError,
    Planner,
)
from gateway.planning.prechecks import PrecheckPolicy, precheck_code
from gateway.runtime.audit import AuditLog
from gateway.runtime.confirmation import (
    PendingConfirmation,
    SourceTrust,
    is_side_effecting,
    provenance_reference,
    reduction_breakdown,
    taint_map,
)
from gateway.runtime.confirmer import (
    CONFIRMATION_POLICIES,
    POLICY_APPROVE,
    POLICY_HUMAN,
    POLICY_REJECT,
)
from gateway.runtime.feedback import (
    ReasonCode,
    assert_safe_suite,
    build_agent_feedback,
    classify_reason,
)
from gateway.runtime.protection import (
    ProtectionInputs,
    ProtectionReport,
    SideChannelPolicy,
    assess,
)

_MAX_PENDING_REAUTHORIZATIONS = 32

# Confirmation-UX versions (the C axis): when/how a human would be shown a held
# call. Orthogonal to the confirmation POLICY (who answers). Invariant: under
# the automatic policies (reject/approve) all UX versions produce identical
# execution results -- deferral exists only to batch HUMAN attention, so with no
# human in the loop every version collapses to an immediate decision.
CONFIRMATION_UX_NONE = "c0"      # no confirmation surface at all
CONFIRMATION_UX_PER_CALL = "c1"  # hold each call individually (confirmation API)
CONFIRMATION_UX_BATCH = "c2"     # batched barrier (prototype: gateway/runtime/batched_confirmation.py)
_CONFIRMATION_UX_VERSIONS = (
    CONFIRMATION_UX_NONE, CONFIRMATION_UX_PER_CALL, CONFIRMATION_UX_BATCH,
)


def _resolve_confirmation_config(
    ux: str | None, policy: str | None
) -> tuple[str, str]:
    """Resolve and validate the (confirmation UX, confirmation policy) pair.

    Explicit arguments win; otherwise ``PAUTH_CONFIRMATION_UX`` /
    ``PAUTH_CONFIRMATION_POLICY``; defaults keep the historical behavior
    (per-call holds awaiting a human: c1 + human).
    """
    import os

    ux = (ux or os.environ.get("PAUTH_CONFIRMATION_UX", CONFIRMATION_UX_PER_CALL)).lower()
    policy = (
        policy or os.environ.get("PAUTH_CONFIRMATION_POLICY", POLICY_HUMAN)
    ).lower()
    if ux not in _CONFIRMATION_UX_VERSIONS:
        raise ValueError(
            f"unknown confirmation UX {ux!r}; known: {list(_CONFIRMATION_UX_VERSIONS)}"
        )
    if policy not in CONFIRMATION_POLICIES:
        raise ValueError(
            f"unknown confirmation policy {policy!r}; known: {sorted(CONFIRMATION_POLICIES)}"
        )
    if ux == CONFIRMATION_UX_NONE and policy == POLICY_HUMAN:
        raise ValueError(
            "confirmation UX 'c0' has no surface to show a human; "
            "pick policy 'reject'/'approve' or UX 'c1'"
        )
    if ux == CONFIRMATION_UX_BATCH and policy == POLICY_HUMAN:
        raise ValueError(
            "confirmation UX 'c2' with a human is the batched-barrier prototype "
            "(gateway/runtime/batched_confirmation.py) and is not integrated into "
            "Gateway yet; pick UX 'c1' for a human, or an automatic policy "
            "(under which c2 collapses to an immediate decision)"
        )
    return ux, policy


def _ordered_tools(rules) -> set[str] | None:
    """Side-effecting tools whose plan order the enforcer must uphold.

    Opt-in via ``PAUTH_ENFORCE_CALL_ORDER=1``: with data-independent
    side-effecting calls (e.g. lower-limit-then-send), program order carries
    meaning that operand matching alone cannot see. Off by default because the
    batched-barrier flow legitimately defers side effects past later reads and
    honours partial rejections, which strict ordering would deny.
    """
    import os

    if os.environ.get("PAUTH_ENFORCE_CALL_ORDER", "").lower() not in {"1", "true", "yes"}:
        return None
    return {r.tool for r in rules if is_side_effecting(r.tool)}


def _confirm_key(value: Any) -> Any:
    """Hashable, TYPE-EXACT key identifying a confirmed operand value.

    Confirmation is an exact-value whitelist, so the key must not collapse
    distinct values into one. Python's ``1 == 1.0 == True`` (and equal hashes)
    would otherwise let an approval of ``1`` bless ``1.0`` or ``True`` -- a value
    the human never saw. Tagging by type keeps ``(int,1)``, ``(float,1.0)`` and
    ``(bool,True)`` distinct set elements.
    """
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (str, int)):
        return (type(value).__name__, value)
    if isinstance(value, float):
        # Bind the IEEE-754 payload, not Python equality: 0.0 and -0.0 compare
        # equal, and NaNs have unusual equality, but approval must match the
        # exact operand bits the user saw.
        return ("float64", struct.pack("!d", value))
    if isinstance(value, bytes):
        return ("bytes", value)
    try:
        hash(value)
        return ("obj", type(value).__name__, value)
    except TypeError:
        return ("repr", repr(value))


def _typed_action_key(value: Any) -> Any:
    """Canonical, type-exact key for a whole explicitly reauthorized action."""
    if value is None:
        return ("none",)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (str, int)):
        return (type(value).__name__, value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite floats cannot be reauthorized")
        return ("float64", struct.pack("!d", value))
    if isinstance(value, bytes):
        return ("bytes", value)
    if isinstance(value, list):
        return ("list", tuple(_typed_action_key(v) for v in value))
    if isinstance(value, tuple):
        return ("tuple", tuple(_typed_action_key(v) for v in value))
    if isinstance(value, dict):
        pairs = [
            (_typed_action_key(k), _typed_action_key(v)) for k, v in value.items()
        ]
        return ("dict", tuple(sorted(pairs, key=repr)))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return (
            "dataclass",
            type(value).__module__,
            type(value).__qualname__,
            _typed_action_key(dataclasses.asdict(value)),
        )
    if hasattr(value, "model_dump"):
        try:
            return (
                "model",
                type(value).__module__,
                type(value).__qualname__,
                _typed_action_key(value.model_dump()),
            )
        except Exception:  # noqa: BLE001
            pass
    raise TypeError(
        "off-plan reauthorization supports only canonical JSON-like, "
        "dataclass, or model operands"
    )


@dataclasses.dataclass
class SubmissionResult:
    """Outcome of the one-shot plan-generation step."""

    accepted: bool
    reason: str
    rule_count: int = 0


class ExecutionStatus(str, enum.Enum):
    """Execution outcome, kept separate from the authorization decision."""

    NOT_DISPATCHED = "not_dispatched"
    SUCCEEDED = "succeeded"
    INDETERMINATE = "indeterminate"


@dataclasses.dataclass
class CallResult:
    """Outcome of one tool call routed through the gateway.

    ``reason`` is the internal/human-facing reason and may contain values.
    ``agent_reason`` is the value-free string safe to surface to the agent's
    model context (see ``gateway/runtime/feedback.py``); it is
    populated on every denial.
    """

    permit: bool
    reason: str
    return_value: Any | None
    agent_reason: str | None = None
    reauthorization_required: bool = False
    reauthorized: bool = False
    authorization_permit: bool = False
    execution_status: ExecutionStatus = ExecutionStatus.NOT_DISPATCHED


@dataclasses.dataclass(frozen=True)
class PendingReauthorization:
    """One known-suite, plan-external action held for a trusted user decision."""

    reauthorization_id: str
    tool: str
    args: tuple[Any, ...]
    plan_source_sha256: str
    action_key: tuple[Any, ...] = dataclasses.field(repr=False)


@dataclasses.dataclass
class _Session:
    prompt: str
    run_doc: dict[str, Any] | None
    rejection_reason: str | None
    enforcer: Enforcer | None
    tool_executor: Callable[[str, dict[str, Any]], Any] | None
    tool_params: dict[str, list[str]]
    generated_code: str | None = None
    execution_plan: ExecutionPlan | None = None
    planner_metadata: dict[str, Any] | None = None
    # Confirmation-gated sinks (primary dangerous-flow closure) -- shared with _CompositeState so the
    # gate runs on the live session path, not only the composite path (S18/S19).
    source_trust: SourceTrust = dataclasses.field(default_factory=SourceTrust)
    docs_by_name: dict[str, Any] = dataclasses.field(default_factory=dict)
    precheck_policy: Any = None
    # Static provenance taint (S20): (tool, param_index) control operands that
    # derive from an untrusted source. Computed once from the plan code.
    gated_operands: set = dataclasses.field(default_factory=set)
    gated_sources: dict = dataclasses.field(default_factory=dict)
    pending: dict[str, PendingConfirmation] = dataclasses.field(default_factory=dict)
    confirmed: set = dataclasses.field(default_factory=set)
    confirm_seq: int = 0
    loop_counts: dict[str, int] = dataclasses.field(default_factory=dict)
    bulk_confirmed: set[str] = dataclasses.field(default_factory=set)
    reauthorization_pending: dict[str, PendingReauthorization] = dataclasses.field(
        default_factory=dict
    )
    reauthorization_grants: set[Any] = dataclasses.field(default_factory=set)
    reauthorization_denied: set[Any] = dataclasses.field(default_factory=set)
    reauthorization_seq: int = 0


@dataclasses.dataclass
class _CompositeState:
    """Runtime state of a composite (staged) plan. See planning/composite.py."""

    prompt: str
    plan: CompositePlan
    tool_executor: Callable[[str, dict[str, Any]], Any]
    tool_params: dict[str, list[str]]
    tool_names: set[str]
    tool_signer: dict[str, str]
    store: EnvelopeStore
    bindings: dict[str, Any]
    stage_idx: int = -1
    enforcer: Enforcer | None = None
    stage_code: str | None = None
    stage_rule_total: int = 0
    consumed: set[str] = dataclasses.field(default_factory=set)
    stage_assignments: dict[str, str] = dataclasses.field(default_factory=dict)
    complete: bool = False
    failure: str | None = None
    any_rules: bool = False
    truncated_total: int = 0
    # Confirmation-gated sinks (primary dangerous-flow closure).
    source_trust: SourceTrust = dataclasses.field(default_factory=SourceTrust)
    docs_by_name: dict[str, Any] = dataclasses.field(default_factory=dict)
    precheck_policy: Any = None
    gated_operands: set = dataclasses.field(default_factory=set)
    gated_sources: dict = dataclasses.field(default_factory=dict)
    pending: dict[str, PendingConfirmation] = dataclasses.field(default_factory=dict)
    confirmed: set = dataclasses.field(default_factory=set)
    confirm_seq: int = 0
    loop_counts: dict[str, int] = dataclasses.field(default_factory=dict)
    bulk_confirmed: set[str] = dataclasses.field(default_factory=set)


class Gateway:
    """Single-task gateway. Plan once, enforce every subsequent call."""

    def __init__(
        self,
        suite_loader: Callable[[str], SuiteSpec],
        precheck_policy: PrecheckPolicy | None = None,
        source_trust: SourceTrust | None = None,
        side_channel_policy: SideChannelPolicy | None = None,
        isolated_runtime: bool = False,
        audit_log: AuditLog | None = None,
        restored_execution_state: dict[str, Any] | None = None,
        execution_state_sink: Callable[[dict[str, Any]], None] | None = None,
        confirmation_ux: str | None = None,
        confirmation_policy: str | None = None,
    ) -> None:
        """``suite_loader(name)`` returns the real-tool ``SuiteSpec`` for ``name``.

        The gateway calls it once per submitted user prompt to wire up the
        environment, the tool runtime and the per-task envelope store.
        ``precheck_policy`` tunes the deterministic precheck gate applied to every
        accepted plan. ``source_trust`` labels which tools
        return untrusted data, driving the confirmation-gated sink (primary dangerous-flow closure).
        """
        self._suite_loader = suite_loader
        self._precheck_policy = precheck_policy
        self._source_trust = source_trust or SourceTrust()
        # Side channels denied by default (the no-raw-side-channels precondition).
        self._side_channel_policy = side_channel_policy or SideChannelPolicy()
        self._isolated_runtime = isolated_runtime
        # Observability / audit (cross-cutting). An injected AuditLog lets a
        # deployment share one persistent trail across sessions (http_server
        # --audit-log); the default is a per-gateway in-memory log. Explicit
        # None check: AuditLog defines __len__, so an empty one is falsy and
        # `audit_log or AuditLog()` would wrongly discard an injected empty log.
        self._audit = audit_log if audit_log is not None else AuditLog()
        self._restored_execution_state = restored_execution_state
        self._execution_state_sink = execution_state_sink
        self._confirmation_ux, self._confirmation_policy = _resolve_confirmation_config(
            confirmation_ux, confirmation_policy
        )
        self._session: _Session | None = None
        self._composite: _CompositeState | None = None
        # Per-Gateway, not process-global: calls in one task are serialized
        # across the complete check -> tool_executor -> record state transition while
        # independent Gateway sessions remain concurrent.
        self._execution_lock = threading.RLock()

    def audit_log(self) -> list:
        """Structured permit/deny/accept/reject events (operator-facing)."""
        return self._audit.events()

    # ------------------------------------------------------------------
    # Plan-generation entrypoint -- driven by the user, never the agent.
    # ------------------------------------------------------------------
    def submit_user_prompt(self, prompt: str) -> SubmissionResult:
        """Translate the user prompt into rules. Called ONCE per task.

        The agent must not have a reference to this method. Any caller that
        invokes it on behalf of the agent re-opens the input-path hole.
        """
        return self._submit_with_planner(prompt, DeterministicRecognizerPlanner())

    def submit_user_prompt_with_planner(
        self,
        prompt: str,
        planner: Planner,
        *,
        generated_code_on_success: bool = True,
    ) -> SubmissionResult:
        """Translate the prompt using an explicit planner strategy."""
        return self._submit_with_planner(
            prompt,
            planner,
            generated_code_on_success=generated_code_on_success,
        )

    # ------------------------------------------------------------------
    # Free-form plan generation -- LLM the Planner over an arbitrary prompt.
    # ------------------------------------------------------------------
    def submit_user_prompt_freeform(
        self,
        prompt: str,
        suite_name: str,
        model: str = "gpt-4.1",
        cache_path: Path | None = None,
        max_retries: int = 3,
        enable_judge: bool = True,
        judge_model: str | None = None,
    ) -> SubmissionResult:
        """Plan generation via LLM, bypassing the deterministic recognizer.

        ``max_retries == 0`` uses the paper-faithful one-shot the Planner from
        ``pauth.codegen``. ``max_retries > 0`` enables the DSL
        self-repair loop in ``gateway.agentic_planner``: each
        ``DSLRejectionError`` is fed back to the LLM with the
        previous (invalid) code and an explicit "you MUST obey rule X"
        instruction.

        Rejection modes are unchanged: unknown suite, the Planner DSL
        violation (after the retry budget), or Slicer/Rule-compiler compilation failure.
        On acceptance, ``handle_tool_call`` enforcement is identical to
        the recognizer path.
        """
        planner = LLMFreeformPlanner(
            suite_name=suite_name,
            model=model,
            cache_path=cache_path,
            max_retries=max_retries,
            enable_judge=enable_judge,
            judge_model=judge_model,
        )
        return self._submit_with_planner(prompt, planner, generated_code_on_success=True)

    # ------------------------------------------------------------------
    # Composite (staged) plan submission.
    # ------------------------------------------------------------------
    def submit_user_prompt_composite(
        self,
        prompt: str,
        plan: CompositePlan,
    ) -> SubmissionResult:
        """Accept a staged plan: per-stage Appendix-A code + gateway-evaluated guards.

        Stage code passes the unmodified ``pauth.prepare``; the composition
        layer only decides *when* each stage's rules are active. Guards and
        fan-out bounds are resolved exclusively from this gateway's own
        signed observations -- never from the agent.
        """
        self._session = None
        self._composite = None
        try:
            suite = self._suite_loader(plan.suite_name)
        except Exception as exc:  # noqa: BLE001
            reason = f"unknown suite {plan.suite_name!r}: {type(exc).__name__}: {exc}"
            self._session = self._rejected_session(prompt, reason)
            return SubmissionResult(accepted=False, reason=reason)

        violations = validate_plan(prompt, plan, suite.tool_docs(), self._precheck_policy)
        if violations:
            reason = "composite plan rejected: " + "; ".join(violations)
            self._session = self._rejected_session(prompt, reason)
            return SubmissionResult(accepted=False, reason=reason)

        env = suite.make_env()
        state = _CompositeState(
            prompt=prompt,
            plan=plan,
            tool_executor=suite.tool_executor_factory(env),
            tool_params=suite.tool_params(),
            tool_names=suite.tool_names(),
            tool_signer=suite.tool_signer(),
            store=EnvelopeStore(KeyRing()),
            bindings={},
            source_trust=self._source_trust,
            docs_by_name={t.name: t for t in suite.tool_docs()},
            precheck_policy=self._precheck_policy,
        )
        error = self._composite_advance(state, initial=True)
        if error:
            self._session = self._rejected_session(prompt, error)
            return SubmissionResult(accepted=False, reason=error)
        if state.complete and not state.any_rules:
            reason = "plan authorizes no tool calls; rejected (default-deny)"
            self._session = self._rejected_session(prompt, reason)
            return SubmissionResult(accepted=False, reason=reason)

        self._composite = state
        reason = f"{plan.reason} ({len(plan.stages)} stages)"
        self._audit.record("submit", "accept", reason_code="accepted", reason=reason)
        return SubmissionResult(
            accepted=True,
            reason=reason,
            rule_count=state.stage_rule_total,
        )

    def _composite_activate(self, state: _CompositeState, idx: int) -> str | None:
        """Instantiate and compile stage ``idx``. Returns an error string on failure."""
        stage = state.plan.stages[idx]
        if stage.fanout is not None:
            list_value = state.bindings[stage.fanout.list_var]
            try:
                inst = instantiate_fanout(stage, list_value)
            except CompositePlanError as exc:
                return f"stage {idx} fan-out instantiation failed: {exc}"
            code = inst.code
            state.truncated_total += inst.truncated
        else:
            code = stage.code
        try:
            prepared = prepare(code, state.tool_names, state.tool_signer)
        except Exception as exc:  # noqa: BLE001
            return f"stage {idx} compilation failed: {type(exc).__name__}: {exc}"
        state.stage_idx = idx
        state.stage_code = code
        state.stage_rule_total = len(prepared.rules)
        state.consumed = set()
        state.stage_assignments = assignment_map(code, state.tool_names)
        # Static provenance taint for this stage's control operands (S20).
        # NOTE: for a fan-out stage the body's observed constants are already
        # folded in, so their provenance is lost here -- fan-out over an
        # untrusted list can under-gate (documented limitation; fan-out is not
        # on the live path yet).
        state.gated_sources = taint_map(
            code, state.docs_by_name, state.source_trust, state.precheck_policy
        )
        state.gated_operands = set(state.gated_sources)
        # Non-accumulation: the previous stage's enforcer is discarded; its
        # rules can never authorize again. The envelope store is shared so
        # later guards/operands still reference earlier signed observations.
        state.enforcer = Enforcer(
            prepared.rules, state.store, state.tool_signer,
            ordered_tools=_ordered_tools(prepared.rules),
        )
        state.any_rules = state.any_rules or bool(prepared.rules)
        return None

    def _composite_advance(self, state: _CompositeState, initial: bool = False) -> str | None:
        """Advance stages while their entry conditions hold. Fail-closed."""
        while not state.complete:
            nxt = state.stage_idx + 1
            if nxt >= len(state.plan.stages):
                if len(state.consumed) >= state.stage_rule_total:
                    state.complete = True
                break
            stage = state.plan.stages[nxt]
            if state.stage_idx >= 0 or not initial:
                # Entry condition for every stage after the first activation.
                if stage.guard is not None:
                    try:
                        if not eval_guard(stage.guard, state.bindings):
                            break
                    except GuardNotEvaluable:
                        break
                elif len(state.consumed) < state.stage_rule_total:
                    break  # unconditional transition requires full consumption
            if stage.fanout is not None and stage.fanout.list_var not in state.bindings:
                break
            error = self._composite_activate(state, nxt)
            if error:
                state.complete = True
                state.failure = error
                state.enforcer = None
                return error
            initial = False
            if state.stage_rule_total > 0:
                break  # agent must act before anything else can change
            # Zero-rule stage (e.g. fan-out over an empty list): fall through
            # and keep advancing.
        return None

    def _handle_tool_call_composite(self, tool: str, args: list[Any]) -> CallResult:
        state = self._composite
        assert state is not None
        if state.failure:
            return CallResult(False, f"default-deny: {state.failure}", None)
        if state.complete or state.enforcer is None:
            return CallResult(False, "composite plan complete (default-deny)", None)

        decision = state.enforcer.check(tool, args, live=True)
        if not decision.permit:
            return CallResult(False, decision.reason, None)
        assert decision.rule is not None
        # A loop (quantified) rule authorises many calls; only NON-loop rules are
        # one-shot in a composite stage.
        if not decision.rule.loops and decision.rule.key in state.consumed:
            return CallResult(
                False,
                f"rule {decision.rule.key} already consumed (composite one-shot)",
                None,
            )

        gate = self._pre_execution_gate(state, decision.rule, tool, args)
        if gate is not None:
            gate.authorization_permit = True
            return gate

        result = self._execute_authorized_tool(
            state,
            tool,
            args,
            decision.reason,
            enforcer=state.enforcer,
            token=decision.token,
        )
        if not result.permit:
            return result
        raw = result.return_value

        record_failure = self._record_authorized_result(
            state.enforcer, decision.rule, decision.token, raw
        )
        if record_failure is not None:
            return record_failure
        state.consumed.add(decision.rule.key)
        # Bind stage variables from the gateway's own observation so later
        # guards evaluate against what *we* saw, not what the agent claims.
        for var, var_tool in state.stage_assignments.items():
            if var_tool == tool and var not in state.bindings:
                state.bindings[var] = raw
        self._composite_advance(state)
        return result

    def _pre_execution_gate(
        self,
        state: "_Session | _CompositeState",
        rule: Any,
        tool: str,
        args: list[Any],
    ) -> CallResult | None:
        """Apply every human gate shared by session and composite plans, then
        the deployment's confirmation policy.

        Policy 'human' (default): return the hold unchanged -- the call waits
        for the confirmation surface. Automatic policies resolve every hold on
        the spot: 'reject' denies the call outright; 'approve' whitelists and
        re-checks until the gates pass (bounded: each round shrinks the set of
        unconfirmed operands). Under automatic policies the UX version has no
        effect on results (see _resolve_confirmation_config).
        """
        while True:
            held = self._confirmation_gate(state, tool, args)
            if held is None:
                held = self._bulk_gate(state, rule, tool)
            if held is None:
                return None
            if self._confirmation_policy == POLICY_HUMAN:
                return held
            resolved = self._auto_resolve_pending(state)
            if self._confirmation_policy == POLICY_REJECT:
                return CallResult(
                    permit=False,
                    reason=(
                        f"confirmation policy 'reject': {tool} call denied "
                        "(untrusted-derived control operand; no human "
                        "confirmation in this deployment)"
                    ),
                    return_value=None,
                )
            if not resolved:  # approve policy but nothing to approve: bail out
                return held

    def _auto_resolve_pending(self, state: "_Session | _CompositeState") -> bool:
        """Resolve every pending confirmation per the automatic policy."""
        approved = self._confirmation_policy == POLICY_APPROVE
        resolved = False
        for cid in list(state.pending):
            resolved = self._confirm_serialized(cid, approved) or resolved
        return resolved

    @staticmethod
    def _execute_authorized_tool(
        state: "_Session | _CompositeState",
        tool: str,
        args: list[Any],
        reason: str,
        *,
        enforcer: Enforcer | None = None,
        token: tuple | None = None,
    ) -> CallResult:
        """Validate, durably reserve, and run an authorized call."""
        params = state.tool_params.get(tool, [])
        if len(params) != len(args):
            return CallResult(
                permit=False,
                reason=f"arity mismatch for {tool}: expected {len(params)}, got {len(args)}",
                return_value=None,
                authorization_permit=True,
            )

        if enforcer is not None:
            try:
                begun = enforcer.begin(token)
            except Exception as exc:  # noqa: BLE001 -- tool_executor must stay untouched
                return CallResult(
                    permit=False,
                    reason=(
                        "execution state error: execution attempt could not be durably "
                        f"recorded: {type(exc).__name__}: {exc}"
                    ),
                    return_value=None,
                    authorization_permit=True,
                )
            if not begun:
                return CallResult(
                    permit=False,
                    reason="execution attempt already exists (replay blocked)",
                    return_value=None,
                    authorization_permit=True,
                )

        tool_executor = state.tool_executor
        assert tool_executor is not None
        try:
            raw = tool_executor(tool, dict(zip(params, args)))
        except Exception as exc:  # noqa: BLE001 -- dispatched outcome is unknown
            persistence_detail = ""
            if enforcer is not None:
                try:
                    enforcer.mark_indeterminate(token)
                except Exception as state_exc:  # pre-run started tombstone remains
                    persistence_detail = (
                        "; indeterminate-state persistence failed: "
                        f"{type(state_exc).__name__}: {state_exc}"
                    )
            return CallResult(
                permit=False,
                reason=(
                    "indeterminate tool outcome: tool_executor raised "
                    f"{type(exc).__name__}: {exc}{persistence_detail}"
                ),
                return_value=None,
                authorization_permit=True,
                execution_status=ExecutionStatus.INDETERMINATE,
            )
        return CallResult(
            permit=True,
            reason=reason,
            return_value=raw,
            authorization_permit=True,
            execution_status=ExecutionStatus.SUCCEEDED,
        )

    @staticmethod
    def _record_authorized_result(
        enforcer: Enforcer,
        rule: Any,
        token: tuple | None,
        raw: Any,
    ) -> CallResult | None:
        """Finalize envelope + attempt state, or retain a fail-closed tombstone."""
        try:
            enforcer.record(rule, wrap(raw), token)
        except Exception as exc:  # noqa: BLE001 -- external effect may already exist
            persistence_detail = ""
            try:
                enforcer.mark_indeterminate(token)
            except Exception as state_exc:
                persistence_detail = (
                    "; indeterminate-state persistence failed: "
                    f"{type(state_exc).__name__}: {state_exc}"
                )
            return CallResult(
                permit=False,
                reason=(
                    "indeterminate tool outcome: result finalization failed: "
                    f"{type(exc).__name__}: {exc}{persistence_detail}"
                ),
                return_value=None,
                authorization_permit=True,
                execution_status=ExecutionStatus.INDETERMINATE,
            )
        return None

    def _confirmation_gate(
        self, state: "_Session | _CompositeState", tool: str, args: list[Any]
    ) -> CallResult | None:
        """Return a PENDING_CONFIRMATION denial if a CONTROL operand is
        untrusted-derived (static provenance taint, S20) and not yet approved.

        Gating is by ``(tool, position)`` -- a value transformed on the way
        (``amount * 2``) cannot launder out of it. Confirmation is keyed by the
        concrete value, so approving one value does not bless a different one.
        The actual value is stored for the human side channel; the returned
        reason is value-free (the agent-facing feedback is built from it, S16).
        """
        # Drive the gate from the gated-operand set directly, so it covers both
        # the narrow (recipient/amount) and broad ("trust the human": any
        # untrusted-derived operand of a side-effecting call) taint modes.
        doc = state.docs_by_name.get(tool)
        for i in sorted(pi for (t, pi) in state.gated_operands if t == tool):
            if i >= len(args):
                continue
            param = doc.parameters[i] if (doc is not None and i < len(doc.parameters)) else {}
            name = param.get("name", str(i))
            key = _confirm_key(args[i])
            if (tool, key) in state.confirmed:
                continue
            existing = next(
                (c for c in state.pending.values()
                 if c.tool == tool and c.param_index == i and _confirm_key(c.value) == key),
                None,
            )
            if existing is None:
                cid = f"c{state.confirm_seq}"
                state.confirm_seq += 1
                # A reduction -> the summand/candidate table; else a bare value ->
                # the provenance (where it was read from) as 参照すべき情報.
                breakdown = provenance = None
                enf = state.enforcer
                if enf is not None:
                    for rule in enf.rules_by_tool.get(tool, []):
                        breakdown = reduction_breakdown(rule, i, enf.store)
                        if breakdown is not None:
                            break
                    if breakdown is None:
                        for rule in enf.rules_by_tool.get(tool, []):
                            provenance = provenance_reference(rule, i, enf.store)
                            if provenance is not None:
                                break
                # If any source of this operand is an LLM extractor (non-re-derivable),
                # flag the value unverifiable -> the warning says "not proven".
                srcs = state.gated_sources.get((tool, i), ())
                unverifiable = any(state.source_trust.is_unverifiable(s) for s in srcs)
                state.pending[cid] = PendingConfirmation(
                    cid, tool, i, name, args[i],
                    source=srcs,
                    param_type=param.get("type", ""),
                    breakdown=breakdown,
                    provenance=provenance,
                    unverifiable=unverifiable,
                )
            return CallResult(
                permit=False,
                reason=(
                    f"pending confirmation for {tool} argument #{i} "
                    "(untrusted-derived control operand)"
                ),
                return_value=None,
            )
        return None

    def _bulk_gate(
        self, state: "_Session | _CompositeState", rule: Any, tool: str
    ) -> CallResult | None:
        """Amplification cap: a plan-authorised loop that exceeds the configured
        iteration cap is held ONCE for human confirmation. Off-plan bulk never
        reaches here (default-denied), so the gate fires only for a genuine task
        loop -- and only past the cap. Approving lets the rest of that loop run."""
        cap = state.source_trust.bulk_max_iterations
        if cap is None or rule is None or not getattr(rule, "loops", None):
            return None
        if rule.key in state.bulk_confirmed:
            return None
        count = state.loop_counts.get(rule.key, 0)
        if count < cap:
            state.loop_counts[rule.key] = count + 1
            return None
        # This call would exceed the cap -> hold the bulk operation once.
        if not any(c.bulk_rule == rule.key for c in state.pending.values()):
            cid = f"c{state.confirm_seq}"
            state.confirm_seq += 1
            state.pending[cid] = PendingConfirmation(
                cid, tool, -1, f"loop {rule.key}", count, bulk_rule=rule.key
            )
        return CallResult(
            permit=False,
            reason=(f"pending confirmation: {tool} loop exceeds {cap} iterations "
                    "(possible amplification -- confirm the bulk operation)"),
            return_value=None,
        )

    def _submit_with_planner(
        self,
        prompt: str,
        planner: Planner,
        *,
        generated_code_on_success: bool = False,
    ) -> SubmissionResult:
        self._composite = None
        try:
            draft = planner.generate(prompt, self._suite_loader)
        except PlanGenerationError as exc:
            reason = str(exc)
            self._session = self._rejected_session(prompt, reason)
            return SubmissionResult(accepted=False, reason=reason)
        except Exception as exc:  # noqa: BLE001
            reason = f"the Planner codegen failed: {type(exc).__name__}: {exc}"
            self._session = self._rejected_session(prompt, reason)
            return SubmissionResult(accepted=False, reason=reason)

        return self._accept_draft(
            prompt,
            draft,
            generated_code_on_success=generated_code_on_success,
        )

    def _accept_draft(
        self,
        prompt: str,
        draft: PlanDraft,
        *,
        generated_code_on_success: bool,
    ) -> SubmissionResult:
        try:
            suite = self._suite_loader(draft.suite_name)
        except Exception as exc:  # noqa: BLE001 -- surface as a clean rejection
            reason = f"unknown suite {draft.suite_name!r}: {type(exc).__name__}: {exc}"
            self._session = self._rejected_session(
                prompt,
                reason,
                run_doc=draft.run_doc,
                generated_code=draft.code if generated_code_on_success else None,
            )
            return SubmissionResult(accepted=False, reason=reason)

        # Registration-time identifier check: reject a suite
        # whose tool/parameter names could carry an injection payload before it
        # can reach agent feedback.
        try:
            assert_safe_suite(suite)
        except ValueError as exc:
            reason = f"suite {draft.suite_name!r} has an unsafe identifier: {exc}"
            self._session = self._rejected_session(
                prompt,
                reason,
                run_doc=draft.run_doc,
                generated_code=draft.code if generated_code_on_success else None,
            )
            return SubmissionResult(accepted=False, reason=reason)

        # Deterministic hard gate: one-sided prechecks run at the accept
        # boundary regardless of which planner (or cache) produced the code, so
        # a stale cache entry or a buggy planner cannot smuggle a fabricated
        # recipient/amount past the gateway.
        violations = precheck_code(
            prompt, draft.code, suite.tool_docs(), policy=self._precheck_policy
        )
        if violations:
            reason = "precheck denied: " + "; ".join(violations)
            self._session = self._rejected_session(
                prompt,
                reason,
                run_doc=draft.run_doc,
                generated_code=draft.code if generated_code_on_success else None,
            )
            return SubmissionResult(accepted=False, reason=reason)

        try:
            prepared = prepare(draft.code, suite.tool_names(), suite.tool_signer())
        except Exception as exc:  # noqa: BLE001
            reason = (
                f"Slicer/Rule-compiler failed: {type(exc).__name__}: {exc}"
                if generated_code_on_success
                else f"plan compilation failed: {type(exc).__name__}: {exc}"
            )
            self._session = self._rejected_session(
                prompt=prompt,
                reason=reason,
                run_doc=draft.run_doc,
                generated_code=draft.code if generated_code_on_success else None,
            )
            return SubmissionResult(accepted=False, reason=reason)

        # An empty plan authorizes nothing. Accepting it would report success
        # for a task the gateway will then deny call-by-call -- and the
        # planners' "def run(): pass" sentinel (emitted when validators never
        # passed) relies on the gateway rejecting here, not on rule_count
        # happening to be zero downstream.
        if not prepared.rules:
            reason = "plan authorizes no tool calls; rejected (default-deny)"
            self._session = self._rejected_session(
                prompt,
                reason,
                run_doc=draft.run_doc,
                generated_code=draft.code if generated_code_on_success else None,
            )
            return SubmissionResult(accepted=False, reason=reason)

        env = suite.make_env()
        keyring = KeyRing()
        store = EnvelopeStore(keyring)
        enforcer = Enforcer(
            prepared.rules, store, suite.tool_signer(),
            ordered_tools=_ordered_tools(prepared.rules),
        )
        try:
            enforcer.configure_execution_state(
                prepared.execution_plan.source_sha256,
                self._restored_execution_state,
                self._execution_state_sink,
            )
        except Exception as exc:  # noqa: BLE001 -- malformed restore must fail closed
            reason = (
                "execution state restore denied (default-deny): "
                f"{type(exc).__name__}: {exc}"
            )
            self._session = self._rejected_session(
                prompt,
                reason,
                run_doc=draft.run_doc,
                generated_code=draft.code if generated_code_on_success else None,
            )
            return SubmissionResult(accepted=False, reason=reason)
        tool_executor = suite.tool_executor_factory(env)

        docs_by_name = {t.name: t for t in suite.tool_docs()}
        _gsrc = taint_map(
            draft.code, docs_by_name, self._source_trust, self._precheck_policy
        )
        self._session = _Session(
            prompt=prompt,
            run_doc=draft.run_doc,
            rejection_reason=None,
            enforcer=enforcer,
            tool_executor=tool_executor,
            tool_params=suite.tool_params(),
            generated_code=draft.code if generated_code_on_success else None,
            execution_plan=prepared.execution_plan,
            planner_metadata=draft.planner_metadata,
            source_trust=self._source_trust,
            docs_by_name=docs_by_name,
            precheck_policy=self._precheck_policy,
            gated_operands=set(_gsrc),
            gated_sources=_gsrc,
        )
        self._audit.record("submit", "accept", reason_code="accepted", reason=draft.reason)
        return SubmissionResult(
            accepted=True,
            reason=draft.reason,
            rule_count=len(prepared.rules),
        )

    def _rejected_session(
        self,
        prompt: str,
        reason: str,
        *,
        run_doc: dict[str, Any] | None = None,
        generated_code: str | None = None,
    ) -> _Session:
        self._audit.record(
            "submit", "reject", reason_code=classify_reason(reason).value, reason=reason
        )
        return _Session(
            prompt=prompt,
            run_doc=run_doc,
            rejection_reason=reason,
            enforcer=None,
            tool_executor=None,
            tool_params={},
            generated_code=generated_code,
        )

    # ------------------------------------------------------------------
    # Enforcement entrypoint -- driven by the agent, per attempted call.
    # ------------------------------------------------------------------
    def handle_tool_call(self, tool: str, args: list[Any]) -> CallResult:
        """Check the call against the fixed plan; execute if permitted.

        The agent's request is the verification *target*, never an input to
        plan generation. Reads and writes share the same path: the gateway
        executes the tool and records the result as an observation envelope,
        so subsequent operand checks reference the gateway's view, not the
        agent's report.

        Every denial is annotated with a value-free ``agent_reason``
        (``gateway/runtime/feedback.py``) that is safe to surface to the
        agent's model context: it carries no attacker-controlled bytes by
        construction.

        The complete live transition is serialized per Gateway. In
        particular, another call cannot pass ``Enforcer.check`` until this
        call has either been held/denied or executed and consumed by
        ``Enforcer.record``.
        """
        with self._execution_lock:
            return self._handle_tool_call_serialized(tool, args)

    def _handle_tool_call_serialized(self, tool: str, args: list[Any]) -> CallResult:
        """Run one tool-call transition while ``_execution_lock`` is held."""
        # Side channels (Bash/shell/exec) are denied unconditionally: the
        # gateway cannot reason about what they do, so it never authorizes them
        # (the no-raw-side-channels precondition). Out-of-band execution that never reaches
        # this method is a separate, integration-level bypass -- reported by
        # protection_report(), not preventable here.
        if self._side_channel_policy.is_denied(tool):
            return self._finalize_agent_reason(
                CallResult(
                    permit=False,
                    reason=f"side channel tool {tool} is not permitted (default-deny)",
                    return_value=None,
                ),
                tool,
                args,
            )
        if self._composite is not None:
            return self._finalize_agent_reason(
                self._handle_tool_call_composite(tool, args), tool, args
            )
        return self._finalize_agent_reason(
            self._handle_tool_call_session(tool, args), tool, args
        )

    def protection_report(self) -> ProtectionReport:
        """Honest effective protection level (L0-L3) and its caveats.

        The in-process gateway captures the clean prompt, routes tool calls, and
        executes tools itself (L3-capable). Side channels are denied when a
        policy is active, but out-of-band execution stays possible unless the
        agent runtime is isolated -- surfaced as a caveat, never hidden.
        """
        return assess(ProtectionInputs(
            captures_clean_prompt=True,
            routes_tool_calls=True,
            gateway_executes_tools=True,
            side_channels_denied=bool(self._side_channel_policy.denied),
            isolated_runtime=self._isolated_runtime,
        ))

    def status(self) -> dict[str, Any]:
        """Value-free operational status for health checks (observable health).

        Carries NO operand value or internal reason text -- only booleans,
        counts, the protection level, and a value-free ``reason_code``. Safe to
        expose on an unauthenticated localhost health endpoint (unlike
        ``audit_log()``, which is operator-facing and may quote values).
        """
        plan_active = False
        rule_count = 0
        reason_code: str | None = None
        if self._composite is not None:
            st = self._composite
            plan_active = (
                not st.complete and st.enforcer is not None and st.failure is None
            )
            rule_count = st.stage_rule_total
            if st.failure:
                reason_code = classify_reason(st.failure).value
        elif self._session is not None:
            se = self._session
            plan_active = se.enforcer is not None
            if se.enforcer is not None:
                rule_count = sum(len(v) for v in se.enforcer.rules_by_tool.values())
            elif se.rejection_reason:
                reason_code = classify_reason(se.rejection_reason).value
        return {
            "plan_active": plan_active,
            "rule_count": rule_count,
            "pending_confirmations": len(self.pending_confirmations()),
            "pending_reauthorizations": len(self.pending_reauthorizations()),
            "audit_events": len(self._audit),
            "reason_code": reason_code,
            "protection": self.protection_report().to_dict(),
        }

    def _finalize_agent_reason(
        self, result: CallResult, tool: str, args: list[Any] | None = None
    ) -> CallResult:
        """Attach a value-free agent-facing reason to any denial, and audit."""
        if not result.permit and result.agent_reason is None:
            result.agent_reason = build_agent_feedback(
                classify_reason(result.reason), tool=tool
            )
        code = classify_reason(result.reason) if not result.permit else ReasonCode.NO_RULE
        if result.permit:
            decision = "permit"
        elif code == ReasonCode.PENDING_CONFIRMATION:
            decision = "pending"
        elif result.execution_status == ExecutionStatus.INDETERMINATE:
            decision = "indeterminate"
        elif code == ReasonCode.EXECUTION_STATE_ERROR:
            decision = "error"
        else:
            decision = "deny"
        self._audit.record(
            "tool_call", decision, tool=tool,
            reason_code=(code.value if not result.permit else "authorized"),
            reason=result.reason,
            args=args,
        )
        return result

    def _handle_tool_call_session(self, tool: str, args: list[Any]) -> CallResult:
        """Session-path enforcement (recognizer / freeform plans)."""
        session = self._session
        if session is None:
            return CallResult(
                permit=False,
                reason="no active session (submit a user prompt first)",
                return_value=None,
            )
        if session.enforcer is None or session.tool_executor is None:
            return CallResult(
                permit=False,
                reason=f"default-deny: {session.rejection_reason}",
                return_value=None,
            )

        reauthorization_key = self._reauthorization_key(session, tool, args)
        if reauthorization_key in session.reauthorization_grants:
            # Consume before executing. A crashing tool or interrupted caller
            # must not turn a single-use grant into a reusable capability.
            session.reauthorization_grants.remove(reauthorization_key)
            result = self._execute_authorized_tool(
                session,
                tool,
                args,
                "explicit one-shot user reauthorization",
            )
            result.reauthorized = True
            return result

        decision = session.enforcer.check(tool, args, live=True)
        if not decision.permit:
            if classify_reason(decision.reason) == ReasonCode.NO_RULE:
                held = self._hold_plan_external_call(
                    session,
                    tool,
                    args,
                    reauthorization_key,
                    decision.reason,
                )
                if held is not None:
                    return held
            return CallResult(permit=False, reason=decision.reason, return_value=None)

        gate = self._pre_execution_gate(session, decision.rule, tool, args)
        if gate is not None:
            gate.authorization_permit = True
            return gate

        result = self._execute_authorized_tool(
            session,
            tool,
            args,
            decision.reason,
            enforcer=session.enforcer,
            token=decision.token,
        )
        if not result.permit:
            return result
        assert decision.rule is not None
        record_failure = self._record_authorized_result(
            session.enforcer,
            decision.rule,
            decision.token,
            result.return_value,
        )
        if record_failure is not None:
            return record_failure
        return result

    @staticmethod
    def _reauthorization_key(
        session: _Session,
        tool: str,
        args: list[Any],
    ) -> tuple[Any, ...] | None:
        source_sha256 = (
            session.execution_plan.source_sha256
            if session.execution_plan is not None
            else ""
        )
        try:
            typed_args = tuple(_typed_action_key(arg) for arg in args)
        except (TypeError, ValueError, OverflowError):
            return None
        return (source_sha256, tool, typed_args)

    def _hold_plan_external_call(
        self,
        session: _Session,
        tool: str,
        args: list[Any],
        action_key: tuple[Any, ...] | None,
        reason: str,
    ) -> CallResult | None:
        """Hold a known-suite off-plan action; never widen policy automatically."""
        params = session.tool_params.get(tool)
        if action_key is None or params is None or len(params) != len(args):
            # Unknown tools and malformed calls are ordinary denials, not
            # candidates a user should be prompted to bless.
            return None
        if action_key in session.reauthorization_denied:
            return CallResult(
                permit=False,
                reason="explicit reauthorization previously denied (default-deny)",
                return_value=None,
            )
        pending = next(
            (
                item
                for item in session.reauthorization_pending.values()
                if item.action_key == action_key
            ),
            None,
        )
        if pending is None:
            if (
                len(session.reauthorization_pending)
                >= _MAX_PENDING_REAUTHORIZATIONS
            ):
                return CallResult(
                    permit=False,
                    reason="reauthorization queue is full (default-deny)",
                    return_value=None,
                )
            reauthorization_id = f"r{session.reauthorization_seq}"
            session.reauthorization_seq += 1
            try:
                args_snapshot = tuple(copy.deepcopy(args))
            except Exception:  # noqa: BLE001 -- uncommon opaque SDK objects
                args_snapshot = tuple(args)
            pending = PendingReauthorization(
                reauthorization_id=reauthorization_id,
                tool=tool,
                args=args_snapshot,
                plan_source_sha256=(
                    session.execution_plan.source_sha256
                    if session.execution_plan is not None
                    else ""
                ),
                action_key=action_key,
            )
            session.reauthorization_pending[reauthorization_id] = pending
        return CallResult(
            permit=False,
            reason=reason,
            return_value=None,
            reauthorization_required=True,
        )

    # ------------------------------------------------------------------
    # Introspection (for the experiment harness, not for the agent).
    # ------------------------------------------------------------------
    def current_plan(self) -> dict[str, Any] | None:
        """Return the active ``run()`` document, or ``None`` if no plan exists.

        Exposed for the experiment harness only. The agent must not see this.
        """
        return self._session.run_doc if self._session else None

    def current_execution_plan(self) -> dict[str, Any] | None:
        """Return the deterministic Compiler-derived execution contract.

        This is operator/experiment introspection. It is deliberately not
        exposed through :class:`AgentChannel`, because the agent only needs the
        permit/deny result, not the complete authorization surface.
        """
        if self._session is None or self._session.execution_plan is None:
            return None
        return self._session.execution_plan.to_dict()

    def current_execution_state(self) -> dict[str, Any] | None:
        """Return the operator-only durable replay/attempt snapshot."""
        if self._session is None or self._session.enforcer is None:
            return None
        return self._session.enforcer.execution_state()

    def current_planner_metadata(self) -> dict[str, Any] | None:
        """Return phase metrics for experiments, never for the agent context."""
        if self._session is None or self._session.planner_metadata is None:
            return None
        return dict(self._session.planner_metadata)

    def current_code(self) -> str | None:
        """Return the active generated code (free-form path), or ``None``."""
        if self._composite is not None:
            return self._composite.stage_code
        return self._session.generated_code if self._session else None

    # ------------------------------------------------------------------
    # Confirmation side channel -- talks to the USER, not the agent.
    # The pending value (possibly poisoned) is shown to the human here; it
    # never re-enters the agent's model context.
    # ------------------------------------------------------------------
    def _active_state(self) -> "_Session | _CompositeState | None":
        """The active plan state, whichever path is live (composite or session)."""
        return self._composite if self._composite is not None else self._session

    def pending_reauthorizations(self) -> list[PendingReauthorization]:
        """Known-suite off-plan actions awaiting a trusted user decision.

        The full operands live only on this operator side channel. Agent-facing
        responses expose the boolean ``reauthorization_required`` and a
        value-free reason, never these values.
        """
        if self._composite is not None or self._session is None:
            return []
        return list(self._session.reauthorization_pending.values())

    def reauthorize(self, reauthorization_id: str, approved: bool) -> bool:
        """Resolve an off-plan action with an exact, single-use grant.

        Approval does not mutate the plan or add a reusable rule. It authorizes
        only a retry with the same tool, type-exact operands and plan digest.
        This Python API is intentionally absent from ``AgentChannel``; only a
        trusted user/operator integration may call it.
        """
        with self._execution_lock:
            return self._reauthorize_serialized(reauthorization_id, approved)

    def _reauthorize_serialized(
        self, reauthorization_id: str, approved: bool
    ) -> bool:
        if self._composite is not None or self._session is None:
            return False
        session = self._session
        pending = session.reauthorization_pending.pop(reauthorization_id, None)
        if pending is None:
            return False
        current_digest = (
            session.execution_plan.source_sha256
            if session.execution_plan is not None
            else ""
        )
        if pending.plan_source_sha256 != current_digest:
            return False
        action_key = pending.action_key
        if approved:
            session.reauthorization_grants.add(action_key)
            decision = "accept"
            reason = "exact off-plan action approved for one retry"
        else:
            session.reauthorization_denied.add(action_key)
            decision = "reject"
            reason = "off-plan action rejected by user"
        self._audit.record(
            "reauthorization",
            decision,
            tool=pending.tool,
            reason_code="explicit_user_decision",
            reason=reason,
            args=list(pending.args),
        )
        return True

    def pending_confirmations(self) -> list[PendingConfirmation]:
        """Held sink calls awaiting user approval (for the side-channel UI)."""
        state = self._active_state()
        if state is None:
            return []
        return list(state.pending.values())

    def confirm(self, confirmation_id: str, approved: bool) -> bool:
        """Resolve a pending confirmation. On approval the held call's value is
        whitelisted so the agent's retry of that exact call proceeds."""
        with self._execution_lock:
            return self._confirm_serialized(confirmation_id, approved)

    def _confirm_serialized(self, confirmation_id: str, approved: bool) -> bool:
        state = self._active_state()
        if state is None or confirmation_id not in state.pending:
            return False
        pc = state.pending.pop(confirmation_id)
        if approved:
            if pc.bulk_rule is not None:
                # Approving the bulk lets the rest of that loop run uncapped.
                state.bulk_confirmed.add(pc.bulk_rule)
            else:
                state.confirmed.add((pc.tool, _confirm_key(pc.value)))
        return True

    def composite_status(self) -> dict[str, Any] | None:
        """Introspection for tests/experiments: staged-plan progress."""
        state = self._composite
        if state is None:
            return None
        return {
            "stage_idx": state.stage_idx,
            "stages": len(state.plan.stages),
            "consumed": len(state.consumed),
            "stage_rule_total": state.stage_rule_total,
            "complete": state.complete,
            "failure": state.failure,
            "truncated_total": state.truncated_total,
        }
