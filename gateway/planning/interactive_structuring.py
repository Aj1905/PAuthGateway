"""P3 / interactive-structuring: clarify missing values with the user, then generate.

Design contract: docs/SYSTEM_MODEL.md 第 2 部ノード 1「P3 / interactive-structuring」.

Flow: (1) an elicitation model inspects the prompt against the tool schema and
either declares it complete (returning a structured prompt with every control
value explicit) or asks targeted questions about missing operands/conditions;
(2) a deployment-supplied ``clarifier`` callback answers those questions by
asking the human; (3) the answers are folded into a structured prompt;
(4) the structured prompt goes through the ordinary code generator; (5) the
generated code enters the normal PAuth pipeline downstream.

Deployment precondition (per the contract): the gateway must own a pre-plan
user dialogue. This module models that as the injectable ``clarifier``
callable. Deployments without one (e.g. today's hooks wire path) still work
for complete prompts; when questions would be needed the planner rejects with
an explicit reason instead of guessing.

Audit requirement: the raw prompt, questions, answers, structured prompt and
generation record are all carried in ``PlanDraft.planner_metadata``.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping

from pauth.codegen import ToolDoc

from .agentic_planner import (
    DEFAULT_JUDGE_MODEL,
    _call_generator,
    _get_generation_client,
    generate_code_with_self_repair,
)

# questions -> answers; supplied by the deployment's interactive surface.
Clarifier = Callable[[list[str]], Mapping[str, str]]

_ELICIT_SYSTEM = """\
You prepare a user's task for strict, literal code generation. The generator
cannot guess: every control value (recipient, amount, id, date, subject,
quantity, branch condition) must be explicit.

Compare the USER TASK against the AVAILABLE TOOLS. Respond with ONE JSON object
and nothing else:

* If any essential control value is missing or ambiguous:
  {"questions": ["<short, specific question>", ...]}
  Ask ONLY about missing or ambiguous control values. Never ask about values
  the task already states. Never invent values.
* Otherwise:
  {"structured_prompt": "<restatement of the task with every control value
  explicit, in one imperative paragraph>"}
"""

_STRUCTURE_INSTRUCTION = """\
USER ANSWERS:
{answers}

Fold these answers into the task. Respond with ONE JSON object and nothing
else: {{"structured_prompt": "<restatement of the task with every control
value explicit>"}}. Do not ask further questions. Never invent values that
neither the task nor the answers state.
"""


class InteractiveStructuringError(Exception):
    """Elicitation or clarification failed; the prompt cannot be structured."""


def _extract_json(text: str) -> dict[str, Any]:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m is None:
        raise InteractiveStructuringError(
            f"elicitation model returned no JSON object: {text[:200]!r}"
        )
    try:
        parsed = json.loads(m.group())
    except json.JSONDecodeError as exc:
        raise InteractiveStructuringError(
            f"elicitation model returned invalid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise InteractiveStructuringError("elicitation JSON is not an object")
    return parsed


def _schema_text(tools: list[ToolDoc]) -> str:
    return "\n".join(t.render() for t in tools)


def structure_prompt(
    prompt: str,
    tools: list[ToolDoc],
    *,
    model: str,
    client: Any,
    clarifier: Clarifier | None,
) -> tuple[str, list[str], dict[str, str]]:
    """Return (structured_prompt, questions, answers); questions may be empty."""
    messages: list[dict[str, str]] = [
        {"role": "system", "content": _ELICIT_SYSTEM},
        {
            "role": "user",
            "content": (
                f"AVAILABLE TOOLS:\n{_schema_text(tools)}\n\nUSER TASK:\n{prompt}"
            ),
        },
    ]
    raw, _, _ = _call_generator(client, model, messages)
    parsed = _extract_json(raw)

    questions = [str(q) for q in parsed.get("questions", []) if str(q).strip()]
    if not questions:
        structured = str(parsed.get("structured_prompt", "")).strip()
        if not structured:
            raise InteractiveStructuringError(
                "elicitation model returned neither questions nor a structured prompt"
            )
        return structured, [], {}

    if clarifier is None:
        raise InteractiveStructuringError(
            "the prompt is missing control values "
            f"({'; '.join(questions)}) and this deployment has no interactive "
            "clarifier configured"
        )
    answers = {str(q): str(a) for q, a in dict(clarifier(questions)).items()}
    answer_lines = "\n".join(f"- Q: {q}\n  A: {a}" for q, a in answers.items())
    messages.append({"role": "assistant", "content": raw})
    messages.append(
        {"role": "user", "content": _STRUCTURE_INSTRUCTION.format(answers=answer_lines)}
    )
    raw2, _, _ = _call_generator(client, model, messages)
    parsed2 = _extract_json(raw2)
    if parsed2.get("questions"):
        raise InteractiveStructuringError(
            "clarification did not converge: the model asked further questions "
            "after the answer round"
        )
    structured = str(parsed2.get("structured_prompt", "")).strip()
    if not structured:
        raise InteractiveStructuringError(
            "clarification round returned no structured prompt"
        )
    return structured, questions, answers


@dataclasses.dataclass(frozen=True)
class InteractiveStructuringPlanner:
    """P3: ask the user for missing values, structure, then generate."""

    suite_name: str
    model: str = "gpt-4.1"
    max_retries: int = 3
    cache_path: Path | None = None
    enable_judge: bool = True
    judge_model: str | None = None
    client: Any | None = None
    judge_client: Any | None = None
    clarifier: Clarifier | None = None

    def generate(self, prompt, suite_loader):
        from .planner import PlanDraft, PlanGenerationError

        try:
            suite = suite_loader(self.suite_name)
        except Exception as exc:  # noqa: BLE001 -- surfaced as a clean rejection
            raise PlanGenerationError(
                f"unknown suite {self.suite_name!r}: {type(exc).__name__}: {exc}"
            ) from exc
        client = _get_generation_client(self.model, self.client)
        try:
            structured, questions, answers = structure_prompt(
                prompt,
                suite.tool_docs(),
                model=self.model,
                client=client,
                clarifier=self.clarifier,
            )
        except InteractiveStructuringError as exc:
            raise PlanGenerationError(str(exc)) from exc

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_retries": self.max_retries,
            "cache_path": self.cache_path,
            "client": client,
            "enable_judge": self.enable_judge,
            "judge_client": self.judge_client,
        }
        if self.judge_model is not None:
            kwargs["judge_model"] = self.judge_model
        else:
            kwargs["judge_model"] = DEFAULT_JUDGE_MODEL
        result = generate_code_with_self_repair(structured, suite.tool_docs(), **kwargs)
        return PlanDraft(
            suite_name=self.suite_name,
            code=result.code,
            reason=(
                f"plan accepted via interactive structuring ({self.suite_name}; "
                f"{len(questions)} clarifying question(s))"
            ),
            planner_metadata={
                "strategy": "interactive-structuring",
                "raw_prompt": prompt,
                "questions": questions,
                "answers": answers,
                "structured_prompt": structured,
                "codegen_attempts": result.attempts,
                "codegen_failure_history": list(result.failure_history),
            },
        )
