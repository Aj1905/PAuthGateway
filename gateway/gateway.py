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

from .planner import (
    DeterministicRecognizerPlanner,
    LLMFreeformPlanner,
    PlanDraft,
    PlanGenerationError,
    Planner,
)


@dataclasses.dataclass
class SubmissionResult:
    """Outcome of the one-shot plan-generation step."""

    accepted: bool
    reason: str
    rule_count: int = 0


@dataclasses.dataclass
class CallResult:
    """Outcome of one tool call routed through the gateway."""

    permit: bool
    reason: str
    return_value: Any | None


@dataclasses.dataclass
class _Session:
    prompt: str
    run_doc: dict[str, Any] | None
    rejection_reason: str | None
    enforcer: Enforcer | None
    runner: Callable[[str, dict[str, Any]], Any] | None
    tool_params: dict[str, list[str]]
    generated_code: str | None = None


class Gateway:
    """Single-task gateway. Plan once, enforce every subsequent call."""

    def __init__(self, suite_loader: Callable[[str], SuiteSpec]) -> None:
        """``suite_loader(name)`` returns the real-tool ``SuiteSpec`` for ``name``.

        The gateway calls it once per submitted user prompt to wire up the
        environment, the tool runtime and the per-task envelope store.
        """
        self._suite_loader = suite_loader
        self._session: _Session | None = None

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

    def _submit_with_planner(
        self,
        prompt: str,
        planner: Planner,
        *,
        generated_code_on_success: bool = False,
    ) -> SubmissionResult:
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

        env = suite.make_env()
        keyring = KeyRing()
        store = EnvelopeStore(keyring)
        enforcer = Enforcer(prepared.rules, store, suite.tool_signer())
        runner = suite.runner_factory(env)

        self._session = _Session(
            prompt=prompt,
            run_doc=draft.run_doc,
            rejection_reason=None,
            enforcer=enforcer,
            runner=runner,
            tool_params=suite.tool_params(),
            generated_code=draft.code if generated_code_on_success else None,
        )
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
        """
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
        return self._session.generated_code if self._session else None
