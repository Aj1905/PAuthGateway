"""P4 / specialized-codegen: restricted-run()-only generation with grammar feedback.

Design contract: docs/SYSTEM_MODEL.md 第 2 部ノード 1「P4 / specialized-codegen」.

The model receives the user prompt and the tool schema and must emit ONLY a
restricted ``run()`` function. Every candidate is checked by the full plan-time
pipeline (grammar validation, slicing, rule compilation via ``pauth.prepare``);
the rejection reason is fed back verbatim as the next attempt's repair input.

Deliberate differences from the llm-freeform/agentic path:

* no semantic judge, no precheck stage, no runtime probe -- the ONLY feedback
  signal is grammar/compile rejection;
* an exhausted retry budget raises ``PlanGenerationError`` -- this planner
  never substitutes a sentinel plan.

The "specialized model" is selected by the ordinary ``model`` parameter
(``PAUTH_PLANNER_MODEL`` on the wire path); a fine-tuned
``prompt + tool schema -> run()`` model plugs in without code changes.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from pauth import prepare
from pauth.codegen import SYSTEM_PROMPT, ToolDoc, _strip_fences, build_user_prompt
from pauth.suites.base import SuiteSpec

from .agentic_planner import (
    _call_generator,
    _get_generation_client,
    _read_cached,
    _rule_reminder,
    _write_cache,
)

_REPAIR_INSTRUCTION = """\
Your previous attempt was REJECTED by the plan-time pipeline.

REJECTION: {error}

Re-emit the `run` function with this violation removed and without introducing
any other restricted-grammar violations.

Reminders for this class of violation:
{rule_reminder}

Output ONLY the corrected code, with no explanation and no markdown fences.
"""


@dataclasses.dataclass
class SpecializedCodegenReport:
    code: str
    attempts: int
    failure_history: list[str]
    prompt_tokens: int
    completion_tokens: int


class SpecializedCodegenError(Exception):
    """The retry budget ran out without a compilable candidate."""


def generate_specialized_code(
    task: str,
    tools: list[ToolDoc],
    *,
    tool_names: set[str],
    tool_signer: dict[str, str],
    model: str = "gpt-4.1",
    max_retries: int = 3,
    cache_path: Path | None = None,
    client: Any | None = None,
) -> SpecializedCodegenReport:
    """Generate restricted code with grammar/compile feedback only."""
    cached = _read_cached(cache_path, model) if cache_path else None
    if cached is not None:
        return SpecializedCodegenReport(
            code=cached.code,
            attempts=cached.attempts,
            failure_history=list(cached.failure_history),
            prompt_tokens=cached.prompt_tokens,
            completion_tokens=cached.completion_tokens,
        )

    client = _get_generation_client(model, client)
    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(task, tools)},
    ]
    failure_history: list[str] = []
    total_pt = total_ct = 0

    for attempt in range(1, max_retries + 2):  # initial + retries
        raw, pt, ct = _call_generator(client, model, messages)
        code = _strip_fences(raw)
        total_pt += pt
        total_ct += ct
        try:
            prepare(code, tool_names, tool_signer)
        except Exception as exc:  # noqa: BLE001 -- grammar/slice/rule rejection
            failure_history.append(f"pipeline: {exc}")
            if attempt > max_retries:
                break
            messages.append({"role": "assistant", "content": code})
            messages.append(
                {
                    "role": "user",
                    "content": _REPAIR_INSTRUCTION.format(
                        error=str(exc), rule_reminder=_rule_reminder(str(exc))
                    ),
                }
            )
            continue
        if cache_path is not None:
            _write_cache(
                cache_path, code, model, total_pt, total_ct, None,
                attempt, failure_history, [],
            )
        return SpecializedCodegenReport(
            code=code,
            attempts=attempt,
            failure_history=failure_history,
            prompt_tokens=total_pt,
            completion_tokens=total_ct,
        )

    raise SpecializedCodegenError(
        f"specialized-codegen exhausted its retry budget "
        f"({max_retries + 1} attempt(s)); last rejection: "
        f"{failure_history[-1] if failure_history else '(none recorded)'}"
    )


@dataclasses.dataclass(frozen=True)
class SpecializedCodegenPlanner:
    """P4: prompt + tool schema -> restricted run() code, grammar feedback only."""

    suite_name: str
    model: str = "gpt-4.1"
    max_retries: int = 3
    cache_path: Path | None = None
    client: Any | None = None

    def generate(self, prompt, suite_loader):
        from .planner import PlanDraft, PlanGenerationError

        try:
            suite: SuiteSpec = suite_loader(self.suite_name)
        except Exception as exc:  # noqa: BLE001 -- surfaced as a clean rejection
            raise PlanGenerationError(
                f"unknown suite {self.suite_name!r}: {type(exc).__name__}: {exc}"
            ) from exc
        try:
            report = generate_specialized_code(
                prompt,
                suite.tool_docs(),
                tool_names=suite.tool_names(),
                tool_signer=suite.tool_signer(),
                model=self.model,
                max_retries=self.max_retries,
                cache_path=self.cache_path,
                client=self.client,
            )
        except SpecializedCodegenError as exc:
            raise PlanGenerationError(str(exc)) from exc
        return PlanDraft(
            suite_name=self.suite_name,
            code=report.code,
            reason=(
                f"plan accepted via specialized codegen ({self.suite_name}; "
                f"attempt {report.attempts})"
            ),
            planner_metadata={
                "strategy": "specialized-codegen",
                "attempts": report.attempts,
                "failure_history": list(report.failure_history),
                "prompt_tokens": report.prompt_tokens,
                "completion_tokens": report.completion_tokens,
            },
        )
