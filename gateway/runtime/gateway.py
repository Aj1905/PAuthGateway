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

import dataclasses
from pathlib import Path
from typing import Any, Callable

from pauth import prepare
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
    control_operands,
    static_taint_map,
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


def _confirm_key(value: Any) -> Any:
    """Hashable key identifying a confirmed operand value (scalars pass through;
    non-scalars fall back to a stable repr)."""
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return value
    try:
        hash(value)
        return value
    except TypeError:
        return repr(value)


@dataclasses.dataclass
class SubmissionResult:
    """Outcome of the one-shot plan-generation step."""

    accepted: bool
    reason: str
    rule_count: int = 0


@dataclasses.dataclass
class CallResult:
    """Outcome of one tool call routed through the gateway.

    ``reason`` is the internal/human-facing reason and may contain values.
    ``agent_reason`` is the value-free string safe to surface to the agent's
    model context (see ``gateway/runtime/feedback.py``, solution.md S16); it is
    populated on every denial.
    """

    permit: bool
    reason: str
    return_value: Any | None
    agent_reason: str | None = None


@dataclasses.dataclass
class _Session:
    prompt: str
    run_doc: dict[str, Any] | None
    rejection_reason: str | None
    enforcer: Enforcer | None
    runner: Callable[[str, dict[str, Any]], Any] | None
    tool_params: dict[str, list[str]]
    generated_code: str | None = None
    # Confirmation-gated sinks (#1 closure) -- shared with _CompositeState so the
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


@dataclasses.dataclass
class _CompositeState:
    """Runtime state of a composite (staged) plan. See planning/composite.py."""

    prompt: str
    plan: CompositePlan
    runner: Callable[[str, dict[str, Any]], Any]
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
    # Confirmation-gated sinks (#1 closure, solution.md S15/S17/S20).
    source_trust: SourceTrust = dataclasses.field(default_factory=SourceTrust)
    docs_by_name: dict[str, Any] = dataclasses.field(default_factory=dict)
    precheck_policy: Any = None
    gated_operands: set = dataclasses.field(default_factory=set)
    gated_sources: dict = dataclasses.field(default_factory=dict)
    pending: dict[str, PendingConfirmation] = dataclasses.field(default_factory=dict)
    confirmed: set = dataclasses.field(default_factory=set)
    confirm_seq: int = 0


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
    ) -> None:
        """``suite_loader(name)`` returns the real-tool ``SuiteSpec`` for ``name``.

        The gateway calls it once per submitted user prompt to wire up the
        environment, the tool runtime and the per-task envelope store.
        ``precheck_policy`` tunes the deterministic Q15-e gate applied to every
        accepted plan (solution.md S1). ``source_trust`` labels which tools
        return untrusted data, driving the confirmation-gated sink (#1 closure,
        solution.md S15/S17).
        """
        self._suite_loader = suite_loader
        self._precheck_policy = precheck_policy
        self._source_trust = source_trust or SourceTrust()
        # Side channels denied by default (Stage 1 禁止前提, #4/B5).
        self._side_channel_policy = side_channel_policy or SideChannelPolicy()
        self._isolated_runtime = isolated_runtime
        # Observability / audit (plan.md 横断). An injected AuditLog lets a
        # deployment share one persistent trail across sessions (http_server
        # --audit-log); the default is a per-gateway in-memory log. Explicit
        # None check: AuditLog defines __len__, so an empty one is falsy and
        # `audit_log or AuditLog()` would wrongly discard an injected empty log.
        self._audit = audit_log if audit_log is not None else AuditLog()
        self._session: _Session | None = None
        self._composite: _CompositeState | None = None

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
    # Free-form plan generation -- LLM A1 over an arbitrary prompt.
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

        ``max_retries == 0`` uses the paper-faithful one-shot A1 from
        ``pauth.codegen``. ``max_retries > 0`` enables the grammar
        self-repair loop in ``gateway.agentic_a1``: each
        ``RestrictedGrammarError`` is fed back to the LLM with the
        previous (invalid) code and an explicit "you MUST obey rule X"
        instruction.

        Rejection modes are unchanged: unknown suite, A1 grammar
        violation (after the retry budget), or A2/A3 compilation failure.
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
    # Composite (staged) plan submission -- solution.md S10/S11.
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
            runner=suite.runner_factory(env),
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
        state.gated_sources = static_taint_map(
            code, state.docs_by_name, state.source_trust, state.precheck_policy
        )
        state.gated_operands = set(state.gated_sources)
        # Non-accumulation: the previous stage's enforcer is discarded; its
        # rules can never authorize again. The envelope store is shared so
        # later guards/operands still reference earlier signed observations.
        state.enforcer = Enforcer(prepared.rules, state.store, state.tool_signer)
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

        decision = state.enforcer.check(tool, args)
        if not decision.permit:
            return CallResult(False, decision.reason, None)
        assert decision.rule is not None
        if decision.rule.key in state.consumed:
            return CallResult(
                False,
                f"rule {decision.rule.key} already consumed (composite one-shot)",
                None,
            )

        # #1 closure: hold the call if a CONTROL operand carries untrusted-derived
        # data that the user has not yet confirmed (solution.md S15/S17).
        gate = self._confirmation_gate(state, tool, list(args))
        if gate is not None:
            return gate

        params = state.tool_params.get(tool, [])
        if len(params) != len(args):
            return CallResult(
                False,
                f"arity mismatch for {tool}: expected {len(params)}, got {len(args)}",
                None,
            )
        try:
            raw = state.runner(tool, dict(zip(params, args)))
        except Exception as exc:  # noqa: BLE001 -- tool-level failure is not a denial
            return CallResult(False, f"tool execution error: {type(exc).__name__}: {exc}", None)

        state.enforcer.record(decision.rule, wrap(raw))
        state.consumed.add(decision.rule.key)
        # Bind stage variables from the gateway's own observation so later
        # guards evaluate against what *we* saw, not what the agent claims.
        for var, var_tool in state.stage_assignments.items():
            if var_tool == tool and var not in state.bindings:
                state.bindings[var] = raw
        self._composite_advance(state)
        return CallResult(True, decision.reason, raw)

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
        for i, name in control_operands(tool, state.docs_by_name, state.precheck_policy):
            if (tool, i) not in state.gated_operands or i >= len(args):
                continue
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
                state.pending[cid] = PendingConfirmation(
                    cid, tool, i, name, args[i],
                    source=state.gated_sources.get((tool, i), ()),
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
            reason = f"A1 codegen failed: {type(exc).__name__}: {exc}"
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

        # Registration-time identifier check (solution.md S16): reject a suite
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

        # Q15-e hard gate: deterministic one-sided prechecks run at the accept
        # boundary regardless of which planner (or cache) produced the code, so
        # a stale cache entry or a buggy planner cannot smuggle a fabricated
        # recipient/amount past the gateway (solution.md S1).
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
                f"A2/A3 failed: {type(exc).__name__}: {exc}"
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
        enforcer = Enforcer(prepared.rules, store, suite.tool_signer())
        runner = suite.runner_factory(env)

        docs_by_name = {t.name: t for t in suite.tool_docs()}
        _gsrc = static_taint_map(
            draft.code, docs_by_name, self._source_trust, self._precheck_policy
        )
        self._session = _Session(
            prompt=prompt,
            run_doc=draft.run_doc,
            rejection_reason=None,
            enforcer=enforcer,
            runner=runner,
            tool_params=suite.tool_params(),
            generated_code=draft.code if generated_code_on_success else None,
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
            runner=None,
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
        construction (solution.md S16).
        """
        # Side channels (Bash/shell/exec) are denied unconditionally: the
        # gateway cannot reason about what they do, so it never authorizes them
        # (#4/B5, Stage 1 禁止前提). Out-of-band execution that never reaches
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
            )
        if self._composite is not None:
            return self._finalize_agent_reason(
                self._handle_tool_call_composite(tool, args), tool
            )
        return self._finalize_agent_reason(
            self._handle_tool_call_session(tool, args), tool
        )

    def protection_report(self) -> ProtectionReport:
        """Honest effective protection level (L0-L3) and its caveats (#4).

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
            "audit_events": len(self._audit),
            "reason_code": reason_code,
            "protection": self.protection_report().to_dict(),
        }

    def _finalize_agent_reason(self, result: CallResult, tool: str) -> CallResult:
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
        else:
            decision = "deny"
        self._audit.record(
            "tool_call", decision, tool=tool,
            reason_code=(code.value if not result.permit else "authorized"),
            reason=result.reason,
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
        if session.enforcer is None or session.runner is None:
            return CallResult(
                permit=False,
                reason=f"default-deny: {session.rejection_reason}",
                return_value=None,
            )

        decision = session.enforcer.check(tool, args)
        if not decision.permit:
            return CallResult(permit=False, reason=decision.reason, return_value=None)

        # #1 closure on the LIVE path: hold the call if a CONTROL operand carries
        # untrusted-derived data the user has not confirmed (same gate as the
        # composite path; unified in S19).
        gate = self._confirmation_gate(session, tool, list(args))
        if gate is not None:
            return gate

        params = session.tool_params.get(tool, [])
        if len(params) != len(args):
            return CallResult(
                permit=False,
                reason=f"arity mismatch for {tool}: expected {len(params)}, got {len(args)}",
                return_value=None,
            )

        kwargs = dict(zip(params, args))
        try:
            raw = session.runner(tool, kwargs)
        except Exception as exc:  # noqa: BLE001 -- tool-level failure is not a denial
            return CallResult(
                permit=False,
                reason=f"tool execution error: {type(exc).__name__}: {exc}",
                return_value=None,
            )

        wrapped = wrap(raw)
        assert decision.rule is not None
        session.enforcer.record(decision.rule, wrapped)
        return CallResult(permit=True, reason=decision.reason, return_value=raw)

    # ------------------------------------------------------------------
    # Introspection (for the experiment runner, not for the agent).
    # ------------------------------------------------------------------
    def current_plan(self) -> dict[str, Any] | None:
        """Return the active ``run()`` document, or ``None`` if no plan exists.

        Exposed for the experiment runner only. The agent must not see this.
        """
        return self._session.run_doc if self._session else None

    def current_code(self) -> str | None:
        """Return the active generated code (free-form path), or ``None``."""
        if self._composite is not None:
            return self._composite.stage_code
        return self._session.generated_code if self._session else None

    # ------------------------------------------------------------------
    # Confirmation side channel -- talks to the USER, not the agent.
    # The pending value (possibly poisoned) is shown to the human here; it
    # never re-enters the agent's model context (solution.md S15/S16/S17).
    # ------------------------------------------------------------------
    def _active_state(self) -> "_Session | _CompositeState | None":
        """The active plan state, whichever path is live (composite or session)."""
        return self._composite if self._composite is not None else self._session

    def pending_confirmations(self) -> list[PendingConfirmation]:
        """Held sink calls awaiting user approval (for the side-channel UI)."""
        state = self._active_state()
        if state is None:
            return []
        return list(state.pending.values())

    def confirm(self, confirmation_id: str, approved: bool) -> bool:
        """Resolve a pending confirmation. On approval the held call's value is
        whitelisted so the agent's retry of that exact call proceeds."""
        state = self._active_state()
        if state is None or confirmation_id not in state.pending:
            return False
        pc = state.pending.pop(confirmation_id)
        if approved:
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
