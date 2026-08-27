"""Pluggable plan generation for the gateway.

This module owns the volatile the Planner boundary: turning a user prompt into the
restricted imperative ``run`` function that PAuth can validate and slice.
Everything downstream of this boundary should stay stable while deployments
swap recognizers, LLM prompts, fine-tuned models, or external planning
services.
"""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path
from typing import Any, Callable, Protocol

from pauth.codegen import generate_code
from pauth.suites.base import SuiteSpec

from .agentic_planner import generate_code_with_self_repair
from .core import recognize_prompt, run_to_pauth_code
from .sufficiency_tightness import (
    SufficiencyTightnessError,
    generate_sufficiency_tightness,
)

# P3-P5 planner classes live in their own modules; imported lazily inside
# build_planner to avoid import cycles (those modules import PlanDraft /
# PlanGenerationError from here).


class PlanGenerationError(Exception):
    """Raised when a planner cannot produce a candidate restricted program."""


STRATEGY_DETERMINISTIC = "deterministic"
STRATEGY_LLM_FREEFORM = "llm-freeform"
STRATEGY_AUTO = "auto"
STRATEGY_INTERACTIVE_STRUCTURING = "interactive-structuring"
STRATEGY_SPECIALIZED_CODEGEN = "specialized-codegen"
STRATEGY_FORMAL_SEMANTIC = "formal-semantic"
STRATEGY_SUFFICIENCY_TIGHTNESS = "sufficiency-tightness"

STRATEGY_ALIASES = {
    "strict": STRATEGY_DETERMINISTIC,
    "recognizer": STRATEGY_DETERMINISTIC,
    "regex": STRATEGY_DETERMINISTIC,
    "freeform": STRATEGY_LLM_FREEFORM,
    "llm_freeform": STRATEGY_LLM_FREEFORM,
    "llm": STRATEGY_LLM_FREEFORM,
    "hybrid": STRATEGY_AUTO,
    "interactive": STRATEGY_INTERACTIVE_STRUCTURING,
    "grill-me": STRATEGY_INTERACTIVE_STRUCTURING,
    "grill_me": STRATEGY_INTERACTIVE_STRUCTURING,
    "specialized": STRATEGY_SPECIALIZED_CODEGEN,
    "specialized_codegen": STRATEGY_SPECIALIZED_CODEGEN,
    "imperative-model": STRATEGY_SPECIALIZED_CODEGEN,
    "formal": STRATEGY_FORMAL_SEMANTIC,
    "formal_semantic": STRATEGY_FORMAL_SEMANTIC,
    "sufficiency_tightness": STRATEGY_SUFFICIENCY_TIGHTNESS,
    "coverage-audit": STRATEGY_SUFFICIENCY_TIGHTNESS,
    "coverage_audit": STRATEGY_SUFFICIENCY_TIGHTNESS,
    "st": STRATEGY_SUFFICIENCY_TIGHTNESS,
}

KNOWN_STRATEGIES = {
    STRATEGY_DETERMINISTIC,
    STRATEGY_LLM_FREEFORM,
    STRATEGY_AUTO,
    STRATEGY_INTERACTIVE_STRUCTURING,
    STRATEGY_SPECIALIZED_CODEGEN,
    STRATEGY_FORMAL_SEMANTIC,
    STRATEGY_SUFFICIENCY_TIGHTNESS,
}
MAX_PLANNER_RETRIES = 20


def _validate_planner_inputs(
    prompt: str,
    model: str,
    max_retries: int,
    suite_name: str | None,
) -> None:
    if not isinstance(prompt, str):
        raise PlanGenerationError("planner prompt must be a string")
    if not isinstance(model, str) or not model:
        raise PlanGenerationError("planner model must be a non-empty string")
    if suite_name is not None and (
        not isinstance(suite_name, str) or not suite_name
    ):
        raise PlanGenerationError("planner suite_name must be a non-empty string")
    if (
        isinstance(max_retries, bool)
        or not isinstance(max_retries, int)
        or not 0 <= max_retries <= MAX_PLANNER_RETRIES
    ):
        raise PlanGenerationError(
            f"max_retries must be an integer from 0 to {MAX_PLANNER_RETRIES}"
        )


def normalize_strategy_name(name: str | None) -> str:
    """Return the canonical planner strategy name."""
    if name is not None and not isinstance(name, str):
        raise PlanGenerationError("planner strategy must be a string")
    raw = (name or STRATEGY_DETERMINISTIC).strip().lower()
    canonical = STRATEGY_ALIASES.get(raw, raw)
    if canonical not in KNOWN_STRATEGIES:
        raise PlanGenerationError(
            f"unknown planner strategy {name!r}; known strategies: {sorted(KNOWN_STRATEGIES)}"
        )
    return canonical


@dataclasses.dataclass(frozen=True)
class PlanDraft:
    """Candidate output of the Planner before grammar/slice/rule compilation."""

    suite_name: str
    code: str
    reason: str
    run_doc: dict | None = None
    planner_metadata: dict[str, Any] | None = None


class Planner(Protocol):
    """A strategy that turns a prompt into a candidate PAuth program."""

    def generate(
        self,
        prompt: str,
        suite_loader: Callable[[str], SuiteSpec],
    ) -> PlanDraft:
        """Return a candidate plan or raise :class:`PlanGenerationError`."""


class DeterministicRecognizerPlanner:
    """Strict path: accept only prompts the local recognizer can prove."""

    def generate(
        self,
        prompt: str,
        suite_loader: Callable[[str], SuiteSpec],
    ) -> PlanDraft:
        run_doc = recognize_prompt(prompt)
        if run_doc is None:
            raise PlanGenerationError("prompt is outside the deterministic recognised subset")
        suite_name = run_doc["suite"]
        return PlanDraft(
            suite_name=suite_name,
            code=run_to_pauth_code(run_doc),
            reason=f"plan accepted ({suite_name}/{run_doc['intent']})",
            run_doc=run_doc,
        )


@dataclasses.dataclass(frozen=True)
class LLMFreeformPlanner:
    """Free-form path: ask the Planner to generate restricted code for one suite."""

    suite_name: str
    model: str = "gpt-4.1"
    cache_path: Path | None = None
    max_retries: int = 3
    enable_judge: bool = True
    judge_model: str | None = None

    def generate(
        self,
        prompt: str,
        suite_loader: Callable[[str], SuiteSpec],
    ) -> PlanDraft:
        _validate_planner_inputs(
            prompt, self.model, self.max_retries, self.suite_name
        )
        try:
            suite = suite_loader(self.suite_name)
        except Exception as exc:  # noqa: BLE001 -- surfaced as a clean rejection
            raise PlanGenerationError(
                f"unknown suite {self.suite_name!r}: {type(exc).__name__}: {exc}"
            ) from exc
        # ``pauth.codegen.generate_code`` is OpenAI-only. Claude/Fable must use
        # the provider-aware generator even for a one-shot Direct@1 run.
        # With zero retries we also disable the embedded semantic judge so the
        # historical "one model generation" meaning remains intact.
        if self.max_retries > 0 or self.model.lower().startswith("claude"):
            kwargs = {
                "model": self.model,
                "max_retries": self.max_retries,
                "cache_path": self.cache_path,
                "enable_judge": self.enable_judge if self.max_retries > 0 else False,
            }
            if self.judge_model is not None:
                kwargs["judge_model"] = self.judge_model
            result = generate_code_with_self_repair(prompt, suite.tool_docs(), **kwargs)
            code = result.code
        else:
            result = generate_code(
                prompt,
                suite.tool_docs(),
                model=self.model,
                cache_path=self.cache_path,
            )
            code = result.code
        return PlanDraft(
            suite_name=self.suite_name,
            code=code,
            reason=f"plan accepted via LLM the Planner ({self.suite_name})",
        )


@dataclasses.dataclass(frozen=True)
class SufficiencyTightnessPlanner:
    """Coverage-first generation followed by a mechanically delete-only audit."""

    suite_name: str
    model: str = "gpt-4.1"
    cache_path: Path | None = None
    max_retries: int = 3
    enable_judge: bool = True
    judge_model: str | None = None
    client: Any | None = None
    judge_client: Any | None = None
    executor: Any | None = None

    def generate(
        self,
        prompt: str,
        suite_loader: Callable[[str], SuiteSpec],
    ) -> PlanDraft:
        try:
            suite = suite_loader(self.suite_name)
        except Exception as exc:  # noqa: BLE001
            raise PlanGenerationError(
                f"unknown suite {self.suite_name!r}: {type(exc).__name__}: {exc}"
            ) from exc
        try:
            result = generate_sufficiency_tightness(
                prompt,
                suite.tool_docs(),
                tool_signer=suite.tool_signer(),
                model=self.model,
                max_retries=self.max_retries,
                cache_path=self.cache_path,
                client=self.client,
                enable_judge=self.enable_judge,
                judge_model=self.judge_model,
                judge_client=self.judge_client,
                executor=self.executor,
            )
        except SufficiencyTightnessError as exc:
            raise PlanGenerationError(str(exc)) from exc
        return PlanDraft(
            suite_name=self.suite_name,
            code=result.code,
            reason=(
                "plan accepted via sufficiency→tightness Planner "
                f"({self.suite_name}; removed {len(result.dropped_action_ids)} "
                "coverage action(s))"
            ),
            planner_metadata=result.metadata(),
        )


@dataclasses.dataclass(frozen=True)
class AutoPlanner:
    """Main ingress strategy: recognizer fast path, then LLM.

    The deterministic recognizer, when it matches, is zero-cost and carries
    the strongest guarantee, so it always runs first. Prompts outside its
    subset fall back to the free-form LLM planner when a suite is configured;
    otherwise the rejection names the missing configuration explicitly.
    """

    freeform: LLMFreeformPlanner | None

    def generate(
        self,
        prompt: str,
        suite_loader: Callable[[str], SuiteSpec],
    ) -> PlanDraft:
        try:
            draft = DeterministicRecognizerPlanner().generate(prompt, suite_loader)
        except PlanGenerationError:
            draft = None
        if draft is not None:
            try:
                suite_loader(draft.suite_name)
            except Exception:  # noqa: BLE001 -- recognized suite not deployed here
                draft = None
        if draft is not None:
            return draft
        if self.freeform is None:
            raise PlanGenerationError(
                "prompt is outside the deterministic recognised subset and no "
                "free-form suite is configured (set PAUTH_PLANNER_SUITE or pass "
                "suite_name)"
            )
        return self.freeform.generate(prompt, suite_loader)


def build_cache_path(
    cache_dir: str | Path | None,
    *,
    strategy: str,
    prompt: str,
    model: str,
    max_retries: int,
    enable_judge: bool = True,
    judge_model: str | None = None,
) -> Path | None:
    """Return a stable per-prompt cache path for generated code."""
    if not cache_dir:
        return None
    slug = hashlib.sha1(
        (
            f"{strategy}::{model}::{prompt}::r{max_retries}"
            f"::judge={enable_judge}:{judge_model or ''}"
        ).encode()
    ).hexdigest()[:12]
    return Path(cache_dir) / f"{slug}.py"


def build_planner(
    strategy: str | None,
    *,
    prompt: str,
    suite_name: str | None = None,
    model: str = "gpt-4.1",
    max_retries: int = 3,
    cache_dir: str | Path | None = None,
    enable_judge: bool = True,
    judge_model: str | None = None,
    clarifier: Callable[[list[str]], dict[str, str]] | None = None,
) -> Planner:
    """Construct a planner strategy by name.

    The names here are product-level strategy names, not class names. Keep this
    function as the single switch point so environment/config selection does
    not leak across the gateway.
    """
    _validate_planner_inputs(prompt, model, max_retries, suite_name)
    canonical = normalize_strategy_name(strategy)
    if canonical == STRATEGY_DETERMINISTIC:
        return DeterministicRecognizerPlanner()
    if canonical in (
        STRATEGY_LLM_FREEFORM,
        STRATEGY_AUTO,
        STRATEGY_SUFFICIENCY_TIGHTNESS,
    ):
        if canonical == STRATEGY_SUFFICIENCY_TIGHTNESS:
            if not suite_name:
                raise PlanGenerationError(
                    "planner strategy 'sufficiency-tightness' requires suite_name"
                )
            return SufficiencyTightnessPlanner(
                suite_name=suite_name,
                model=model,
                cache_path=build_cache_path(
                    cache_dir,
                    strategy=STRATEGY_SUFFICIENCY_TIGHTNESS,
                    prompt=prompt,
                    model=model,
                    max_retries=max_retries,
                    enable_judge=enable_judge,
                    judge_model=judge_model,
                ),
                max_retries=max_retries,
                enable_judge=enable_judge,
                judge_model=judge_model,
            )
        freeform: LLMFreeformPlanner | None = None
        if suite_name:
            freeform = LLMFreeformPlanner(
                suite_name=suite_name,
                model=model,
                cache_path=build_cache_path(
                    cache_dir,
                    strategy=STRATEGY_LLM_FREEFORM,
                    prompt=prompt,
                    model=model,
                    max_retries=max_retries,
                    enable_judge=enable_judge,
                    judge_model=judge_model,
                ),
                max_retries=max_retries,
                enable_judge=enable_judge,
                judge_model=judge_model,
            )
        if canonical == STRATEGY_AUTO:
            return AutoPlanner(freeform=freeform)
        if freeform is None:
            raise PlanGenerationError("planner strategy 'llm-freeform' requires suite_name")
        return freeform
    if canonical == STRATEGY_SPECIALIZED_CODEGEN:
        from .specialized_codegen import SpecializedCodegenPlanner

        if not suite_name:
            raise PlanGenerationError(
                "planner strategy 'specialized-codegen' requires suite_name"
            )
        return SpecializedCodegenPlanner(
            suite_name=suite_name,
            model=model,
            max_retries=max_retries,
            cache_path=build_cache_path(
                cache_dir,
                strategy=STRATEGY_SPECIALIZED_CODEGEN,
                prompt=prompt,
                model=model,
                max_retries=max_retries,
                enable_judge=False,
            ),
        )
    if canonical == STRATEGY_FORMAL_SEMANTIC:
        from .formal_semantic import FormalSemanticPlanner

        if not suite_name:
            raise PlanGenerationError(
                "planner strategy 'formal-semantic' requires suite_name"
            )
        return FormalSemanticPlanner(suite_name=suite_name)
    if canonical == STRATEGY_INTERACTIVE_STRUCTURING:
        from .interactive_structuring import InteractiveStructuringPlanner

        if not suite_name:
            raise PlanGenerationError(
                "planner strategy 'interactive-structuring' requires suite_name"
            )
        return InteractiveStructuringPlanner(
            suite_name=suite_name,
            model=model,
            max_retries=max_retries,
            cache_path=build_cache_path(
                cache_dir,
                strategy=STRATEGY_INTERACTIVE_STRUCTURING,
                prompt=prompt,
                model=model,
                max_retries=max_retries,
                enable_judge=enable_judge,
                judge_model=judge_model,
            ),
            enable_judge=enable_judge,
            judge_model=judge_model,
            clarifier=clarifier,
        )
    raise PlanGenerationError(
        f"planner strategy {canonical!r} has no constructor in build_planner"
    )
