"""Pluggable plan generation for the gateway.

This module owns the volatile A1 boundary: turning a user prompt into the
restricted imperative ``run`` function that PAuth can validate and slice.
Everything downstream of this boundary should stay stable while deployments
swap recognizers, LLM prompts, fine-tuned models, or external planning
services.
"""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path
from typing import Callable, Protocol

from pauth.codegen import generate_code
from pauth.suites.base import SuiteSpec

from .agentic_a1 import generate_code_with_self_repair
from .core import recognize_prompt, run_to_pauth_code


class PlanGenerationError(Exception):
    """Raised when a planner cannot produce a candidate restricted program."""


STRATEGY_DETERMINISTIC = "deterministic"
STRATEGY_LLM_FREEFORM = "llm-freeform"
STRATEGY_AUTO = "auto"
STRATEGY_INTERACTIVE_STRUCTURING = "interactive-structuring"
STRATEGY_SPECIALIZED_CODEGEN = "specialized-codegen"
STRATEGY_FORMAL_SEMANTIC = "formal-semantic"

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
}

KNOWN_STRATEGIES = {
    STRATEGY_DETERMINISTIC,
    STRATEGY_LLM_FREEFORM,
    STRATEGY_AUTO,
    STRATEGY_INTERACTIVE_STRUCTURING,
    STRATEGY_SPECIALIZED_CODEGEN,
    STRATEGY_FORMAL_SEMANTIC,
}


def normalize_strategy_name(name: str | None) -> str:
    """Return the canonical planner strategy name."""
    raw = (name or STRATEGY_DETERMINISTIC).strip().lower()
    canonical = STRATEGY_ALIASES.get(raw, raw)
    if canonical not in KNOWN_STRATEGIES:
        raise PlanGenerationError(
            f"unknown planner strategy {name!r}; known strategies: {sorted(KNOWN_STRATEGIES)}"
        )
    return canonical


@dataclasses.dataclass(frozen=True)
class PlanDraft:
    """Candidate output of A1 before grammar/slice/rule compilation."""

    suite_name: str
    code: str
    reason: str
    run_doc: dict | None = None


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
    """Free-form path: ask A1 to generate restricted code for one suite."""

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
        try:
            suite = suite_loader(self.suite_name)
        except Exception as exc:  # noqa: BLE001 -- surfaced as a clean rejection
            raise PlanGenerationError(
                f"unknown suite {self.suite_name!r}: {type(exc).__name__}: {exc}"
            ) from exc
        if self.max_retries > 0:
            kwargs = {
                "model": self.model,
                "max_retries": self.max_retries,
                "cache_path": self.cache_path,
                "enable_judge": self.enable_judge,
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
            reason=f"plan accepted via LLM A1 ({self.suite_name})",
        )


@dataclasses.dataclass(frozen=True)
class AutoPlanner:
    """Main ingress strategy (docs/solution.md S2): recognizer fast path, then LLM.

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


@dataclasses.dataclass(frozen=True)
class NotYetImplementedPlanner:
    """Registered strategy slot that intentionally fails until implemented."""

    strategy: str

    def generate(
        self,
        prompt: str,
        suite_loader: Callable[[str], SuiteSpec],
    ) -> PlanDraft:
        raise PlanGenerationError(
            f"planner strategy {self.strategy!r} is registered but not implemented yet"
        )


def build_cache_path(
    cache_dir: str | Path | None,
    *,
    strategy: str,
    prompt: str,
    model: str,
    max_retries: int,
) -> Path | None:
    """Return a stable per-prompt cache path for generated code."""
    if not cache_dir:
        return None
    slug = hashlib.sha1(
        f"{strategy}::{model}::{prompt}::r{max_retries}".encode()
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
) -> Planner:
    """Construct a planner strategy by name.

    The names here are product-level strategy names, not class names. Keep this
    function as the single switch point so environment/config selection does
    not leak across the gateway.
    """
    canonical = normalize_strategy_name(strategy)
    if canonical == STRATEGY_DETERMINISTIC:
        return DeterministicRecognizerPlanner()
    if canonical in (STRATEGY_LLM_FREEFORM, STRATEGY_AUTO):
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
    return NotYetImplementedPlanner(canonical)
