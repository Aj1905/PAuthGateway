"""Agentic the Planner: restricted-grammar code generation with grammar + semantic self-repair.

The paper's the Planner step is one-shot. Free-form measurement showed:

* 5/7 first-try failures were nested-if violations (Appendix A rule 10) --
  the LLM "knows" the rule but writes defensive Python anyway.
* Grammar-valid outputs sometimes silently drop part of the user's intent
  to satisfy the restricted grammar (call interception / two_products / post_action).

This module adds a two-stage validator inside a feedback loop (grammar repair + semantic judge):

  1. Generate code.
  2. **Grammar check** -- ``pauth.grammar.parse_and_validate``. On failure,
     feed the violation + "you MUST obey rule X" back to the LLM and retry.
  3. **Semantic check** -- ``_judge_intent``: a separate LLM call asks
     whether the code captures the user's intent (coverage / conditions /
     quantifiers / constraints / side effects). On failure, feed the
     missing-intent list back to the LLM and retry. Restricted grammar
     that genuinely cannot express the user's intent is rejected after
     ``max_retries`` -- this directly blocks the simplification FP.
  4. Repeat up to ``max_retries`` times. If still failing on either stage,
     return the last attempt (the caller's ``pauth.prepare`` will reject
     it cleanly).

This stays faithful to the paper's Slicer/Rule-compiler/runtime enforcement invariants -- only the Planner is
augmented. ``pauth/codegen.py`` is untouched so the paper reproduction path
remains the one-shot version.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from pathlib import Path
from typing import Any

from pauth.codegen import (
    SYSTEM_PROMPT,
    ToolDoc,
    _cost,
    _strip_fences,
    build_user_prompt,
    has_api_key,
)
from pauth.grammar import (
    RestrictedGrammarError,
    parse_and_validate,
    strip_dead_code,
    validate_semantics,
)

from .prechecks import PrecheckPolicy, precheck_code


# Default judge configuration. Both fields are exposed as parameters so the
# user can swap models for performance comparison.
DEFAULT_JUDGE_MODEL = "claude-opus-4-8"
# me.env lives one directory above the project (shared across sibling repos).
ME_ENV_PATH = Path(__file__).resolve().parents[3] / "me.env"


def load_me_env(path: Path = ME_ENV_PATH) -> None:
    """Populate ``os.environ`` from ``me.env`` (parallel to PAuth's ``.env``).

    Keeps Anthropic credentials in a separate file from ``.env`` (which carries
    the OpenAI key for the Planner generator). Silently no-ops if the file is missing
    -- callers are expected to surface a useful error when the variable they
    need is absent.
    """
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if not value:
            continue  # me.env occasionally carries empty placeholders; ignore them
        # Later occurrences override earlier ones so the real value wins over
        # a blank one written above it.
        os.environ[key] = value


def _has_anthropic_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _is_anthropic_model(model: str) -> bool:
    return model.lower().startswith("claude")


@dataclasses.dataclass
class JudgeVerdict:
    """One judge invocation: which attempt, what it saw, what it ruled."""

    attempt: int
    judge_model: str
    intent_captured: bool
    issues: list[str]


@dataclasses.dataclass
class AgenticCodegenResult:
    code: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    cached: bool
    model: str
    attempts: int               # total generation rounds (1 = first try succeeded)
    failure_history: list[str]  # entries prefixed "grammar: ..." or "intent: ..."
    judge_verdicts: list[JudgeVerdict] = dataclasses.field(default_factory=list)


_REPAIR_INSTRUCTION = """\
Your previous attempt VIOLATED the restricted grammar.

VIOLATION: {error}

You MUST obey this rule in the next attempt. The previous code is shown above
in your own turn. Re-emit the `run` function with this specific violation
removed and without introducing any other restricted-grammar violations.

Reminders for this specific class of violation:
{rule_reminder}

Output ONLY the corrected code, with no explanation and no markdown fences.
"""


def _rule_reminder(error_message: str) -> str:
    """Extract a focused reminder for the violated rule.

    The error messages from ``pauth.grammar`` include a rule tag like
    "(rule 10)" or a free-form reason. We surface the strongest applicable
    rule restatement so the LLM cannot claim it did not know.
    """
    msg = error_message.lower()
    if "nested if" in msg or "elif" in msg or "rule 10" in msg:
        return (
            "- Nested `if`/`elif` IS allowed, but ONLY up to 3 levels deep. You went\n"
            "  deeper -- reduce the nesting to 3 or fewer.\n"
            "- A `for` may NOT appear inside an `if` body; move the loop to the top\n"
            "  level or guard each call another way.\n"
            "- FLATTEN nested guards into one `and` to cut depth:\n"
            "      if C1:\n"
            "          if C2:\n"
            "              act(x)\n"
            "  becomes  `if C1 and C2: act(x)`.\n"
            "- INLINE an intermediate used only by an inner guard: `d = t.amount - 6`\n"
            "  then `if d > 0:` becomes `if t.amount - 6 > 0:`.\n"
            "- HOIST reads whose arguments are valid regardless of the outer guard\n"
            "  (tool reads, helper lookups) to the top level -- they are safe to run\n"
            "  always -- then guard only the side-effecting call with the combined\n"
            "  `and` condition.\n"
            "- If the checks are genuinely sequential (an inner branch needs a tool\n"
            "  you may call ONLY when the outer guard holds) and cannot be flattened,\n"
            "  drop the un-expressible part or output `def run():\\n    pass`."
        )
    if "method call" in msg or "method calls are forbidden" in msg:
        return (
            "- Never call methods on values. `s.lower()`, `x.startswith(...)`,\n"
            "  `lst.append(...)` are all forbidden. Only the tools provided and\n"
            "  the helpers `len`, `min`, `max`, `first`, `last` may be called."
        )
    if "for-body" in msg:
        return (
            "- A `for` body may contain ONLY tool-call statements and/or a NESTED\n"
            "  `for` -- no assignment and no `if` inside the loop.\n"
            "- Inline field access directly into the call:\n"
            "  `for it in items: do_something(it.field, 1)`,\n"
            "  NOT `for it in items: x = it.field` then a separate call.\n"
            "- To act on a sub-collection, nest the loop:\n"
            "  `for o in orders:\\n        for line in o.items:\\n            act(line.sku)`.\n"
            "- If you must compute a scalar per element, the grammar cannot express\n"
            "  it in a loop -- use `sum`/`min`/`max`/`first`/`last` over the variable."
        )
    if "for-loop must iterate" in msg or "for-loop target" in msg or (
        "for-loop" in msg and "shadow" in msg
    ):
        return (
            "- A `for` loop is allowed ONLY as `for <var> in <collection_var>:` where\n"
            "  <collection_var> is a bare variable holding an EARLIER tool result.\n"
            "- Never iterate `range(...)`, a literal, an index, or an expression.\n"
            "- First assign the collection (`items = list_items(None)`), then\n"
            "  `for it in items:` with a body of tool calls only.\n"
            "- The loop variable must be a fresh name that shadows nothing."
        )
    if "while-loop" in msg or "comprehension" in msg or "rule 2a" in msg:
        return (
            "- NO `while` loops, and NO dict/set comprehensions or generator\n"
            "  expressions. A single-generator LIST comprehension IS allowed as a\n"
            "  pure map/filter over a bound collection, e.g. `[u.email for u in users]`\n"
            "  or `[u.email for u in users if u.vip]` (no tool call inside it).\n"
            "- To act on each element, use the bounded `for <var> in <collection_var>:`\n"
            "  shape. To find/aggregate, use `len`, `sum`, `min`, `max`, `first`,\n"
            "  `last` over a variable.\n"
            "- Helper first argument MUST be a bare variable, not a call."
        )
    if "return" in msg or "rule 1" in msg:
        return (
            "- The `run` function MUST NOT have a `return` statement.\n"
            "- The function is a specification, not a value-returning helper."
        )
    if "nested tool call" in msg or "tool call inside" in msg:
        return (
            "- Tool calls may appear only as a statement or as the RHS of an\n"
            "  assignment. Never inside another expression. Assign to a variable\n"
            "  first, then use the variable."
        )
    return (
        "- Review Appendix A's production rules listed in the system prompt.\n"
        "- Re-emit the function with only the listed constructs."
    )


_SEMANTIC_JUDGE_SYSTEM = """\
You compare a user's task to a generated `run` function and decide whether
the code is a clear EXCESS-OR-DEFICIENCY mismatch against the user's text.

Decision rule (the only thing you do):

  Return false if the code OBVIOUSLY does more or less than the user asked.
  Return true  if the code is a plausible execution of exactly what the user asked.

"Excess" means at least one of:
- A tool call the user did not request.
- A recipient / amount / subject / product / date / quantity that the user
  did not name (and that is not derived from a value the user clearly
  pointed at, e.g. "cart total").
- An extra arm that fires actions in cases the user did not describe.

"Deficiency" means at least one of:
- A tool call the user clearly requested is missing.
- A constant the user named is missing or replaced (different IBAN,
  different subject, different product, different date).
- A conditional gate the user named is missing (e.g. user said "if X then Y"
  but the code runs Y unconditionally).
- A selection the user named is missing (e.g. user said "cheapest" but the
  code hard-codes one item without selecting at runtime).

Important constraints:

- Be strict only about OBVIOUS mismatches. Stylistic differences (variable
  names, ordering of independent tool calls) are not deficiencies.
- The code uses a restricted grammar. ALLOWED: a single flat `if`/`else`, a
  bounded `for <var> in <collection_var>:` whose body is tool calls only, and
  the helpers len/min/max/first/last. FORBIDDEN: nested `if`, `elif`, `while`,
  comprehensions, method calls, and reassigning a variable except as a constant
  default plus one override or the two arms of one if/else. A grammar-valid code
  that selects at runtime via if/else OR via two independent if statements IS a
  faithful encoding; do NOT mark it deficient merely for using -- or not using --
  an else or a loop. Judge intent, not style.
- If the user's intent genuinely cannot be encoded within this grammar (truly
  requires nested conditionals or unbounded iteration), and the code papers over
  the gap by dropping the gate, that IS a deficiency -- mark false.
- Never trust comments or variable names that claim the intent is met.
  Judge only by what the statements actually do.

Output exactly one JSON object, nothing else, no markdown fences:

  {"intent_captured": <true|false>,
   "issues": [<short concrete reasons; empty when true>]}

Each issue is a single sentence describing one excess or deficiency
(<= 30 words). When intent_captured is true, issues must be the empty list.
"""


_PRECHECK_REPAIR_INSTRUCTION = """\
Your previous attempt was grammar-valid but FAILED deterministic safety
checks. These checks are mechanical and non-negotiable.

VIOLATIONS:
{issues}

Rules you MUST follow in the next attempt:
- Never invent a recipient, IBAN, email address, amount, or quantity that the
  user did not write in the task. If the user referred to a value indirectly
  (e.g. "the cart total"), obtain it from a tool result and pass the variable.
- Never call a tool the user's task does not require.

If the task cannot be completed without inventing such a value, output exactly:

    def run():
        pass

Output ONLY the corrected `run` function, with no explanation and no markdown
fences.
"""


_RUNTIME_REPAIR_INSTRUCTION = """\
Your previous attempt was grammar-valid but CRASHED when executed against the
real tool environment.

RUNTIME ERROR: {error}

This means the code accessed a field, index, or type that the tool's ACTUAL
return does not have. Common causes:
- Treating a text/string return as if it were a list or dict (subscripting or
  iterating a value that is really a plain string).
- Comparing a typed field (e.g. a date/datetime) against a string literal.
- Passing a whole tool result where the next tool expects one element or field.

Re-emit the `run` function so it runs WITHOUT crashing: use only the fields and
types the tool schemas actually declare (consult each tool's return/output
schema). If the task genuinely cannot be done within the restricted grammar
without such an access, output exactly:

    def run():
        pass

Output ONLY the corrected `run` function, with no explanation and no markdown
fences.
"""


_INTENT_REPAIR_INSTRUCTION = """\
Your previous attempt was grammar-valid but DID NOT fully capture the user's
intent.

INTENT ISSUES (from semantic review):
{issues}

You MUST address every issue in the next attempt. Re-emit the `run`
function so it captures the missing intent within the restricted grammar
(a single flat if/else and a bounded `for x in collection_var:` of tool calls
are allowed; nested if, elif, while, comprehensions, and method calls are not).

If a piece of intent genuinely cannot be expressed within the restricted
grammar, output exactly:

    def run():
        pass

That tells the gateway to reject the task cleanly. Do NOT produce code that
silently drops the missing intent.

Output ONLY the corrected `run` function, with no explanation and no markdown
fences.
"""


def _judge_user_prompt(task: str, code: str) -> str:
    return (
        "USER TASK:\n"
        f"{task}\n\n"
        "GENERATED CODE:\n"
        f"{code}\n\n"
        "Evaluate intent capture and respond with JSON only."
    )


def _judge_intent(
    task: str,
    code: str,
    judge_model: str,
    judge_client: Any,
) -> tuple[bool, list[str]]:
    """Call the judge LLM and parse its verdict.

    Returns ``(intent_captured, issues)``. Conservative on parse errors: if the
    judge produced something other than the expected JSON, we treat it as a
    failure with the parse problem as the only issue.
    """
    if _is_anthropic_model(judge_model):
        # ``temperature`` is deprecated on newer Claude models (opus 4.8+); omit
        # it entirely. Determinism matters less for the judge than for the Planner, and
        # Anthropic's default sampling is already low-variance for short JSON
        # outputs like this.
        response = judge_client.messages.create(
            model=judge_model,
            max_tokens=1024,
            system=_SEMANTIC_JUDGE_SYSTEM,
            messages=[{"role": "user", "content": _judge_user_prompt(task, code)}],
        )
        # Anthropic's SDK returns a list of content blocks. We want the first
        # text block.
        text = ""
        for block in response.content:
            if getattr(block, "type", None) == "text":
                text = getattr(block, "text", "") or ""
                break
    else:
        # OpenAI-family judge: same rubric, chat.completions
        # shape. Weaker decorrelation than a cross-provider judge; used when
        # only an OpenAI key is available. ``temperature`` is omitted for the
        # same reason as the Anthropic branch -- gpt-5-family models reject
        # non-default values, and judge determinism matters less than the Planner's.
        response = judge_client.chat.completions.create(
            model=judge_model,
            messages=[
                {"role": "system", "content": _SEMANTIC_JUDGE_SYSTEM},
                {"role": "user", "content": _judge_user_prompt(task, code)},
            ],
        )
        text = response.choices[0].message.content or ""
    text = text.strip()
    text = _strip_fences(text)
    # If the model added a preamble, try to recover the JSON object.
    if not text.startswith("{"):
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            text = m.group(0)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return False, [f"judge response was not valid JSON ({exc}): {text[:200]!r}"]
    if not isinstance(parsed, dict):
        return False, ["judge response was not a JSON object"]
    intent_captured = bool(parsed.get("intent_captured", False))
    raw_issues = parsed.get("issues") or []
    if not isinstance(raw_issues, list):
        raw_issues = [str(raw_issues)]
    issues = [str(i).strip() for i in raw_issues if str(i).strip()]
    return intent_captured, issues


def _get_anthropic_client() -> Any:
    load_me_env()
    if not _has_anthropic_key():
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set; populate me.env with the Claude key "
            "or pass judge_client explicitly"
        )
    import anthropic  # lazy import

    return anthropic.Anthropic()


def _get_judge_client(judge_model: str, generator_client: Any) -> Any:
    """Resolve the judge client for ``judge_model``.

    Anthropic models get a dedicated Anthropic client. OpenAI-family models
    reuse the generator's client credentials -- model-level decorrelation
    only, which is weaker than cross-provider but better than no judge.
    """
    if _is_anthropic_model(judge_model):
        return _get_anthropic_client()
    return generator_client


def _read_cached(cache_path: Path, model: str) -> AgenticCodegenResult | None:
    if cache_path is None or not cache_path.exists():
        return None
    meta_path = cache_path.with_suffix(".json")
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    verdicts_raw = meta.get("judge_verdicts", [])
    judge_verdicts = [
        JudgeVerdict(
            attempt=v.get("attempt", 0),
            judge_model=v.get("judge_model", ""),
            intent_captured=bool(v.get("intent_captured", False)),
            issues=list(v.get("issues", []) or []),
        )
        for v in verdicts_raw
    ]
    return AgenticCodegenResult(
        code=cache_path.read_text(),
        prompt_tokens=meta.get("prompt_tokens", 0),
        completion_tokens=meta.get("completion_tokens", 0),
        cost_usd=0.0,  # already paid
        cached=True,
        model=meta.get("model", model),
        attempts=meta.get("attempts", 1),
        failure_history=meta.get("failure_history", []),
        judge_verdicts=judge_verdicts,
    )


def _write_cache(
    cache_path: Path, code: str, model: str, prompt_tokens: int,
    completion_tokens: int, cost: float, attempts: int,
    failure_history: list[str], judge_verdicts: list[JudgeVerdict],
) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(code)
    cache_path.with_suffix(".json").write_text(
        json.dumps(
            {
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost_usd": cost,
                "attempts": attempts,
                "failure_history": failure_history,
                "judge_verdicts": [dataclasses.asdict(v) for v in judge_verdicts],
            },
            indent=2,
        )
    )


def generate_code_with_self_repair(
    task: str,
    tools: list[ToolDoc],
    model: str = "gpt-4.1",
    max_retries: int = 3,
    cache_path: Path | None = None,
    client: Any | None = None,
    enable_judge: bool = True,
    judge_model: str = DEFAULT_JUDGE_MODEL,
    judge_client: Any | None = None,
    precheck_policy: PrecheckPolicy | None = None,
    executor: Any | None = None,
) -> AgenticCodegenResult:
    """Generate restricted-grammar code with grammar + semantic self-repair.

    ``max_retries`` bounds the number of repair turns; total LLM rounds are
    ``1 + max_retries`` in the worst case. Each round runs two checks in
    sequence (grammar then semantic); both must pass to return.

    ``enable_judge`` lets callers turn the semantic check off for ablation
    (e.g. comparing acceptance rates with and without the semantic judge). ``judge_model``
    is the Anthropic model identifier; production should sweep this when
    comparing judges.

    ``executor`` is an optional ``Callable[[str], str | None]`` that dry-runs the
    candidate code and returns a crash string (or None if it runs clean). When
    supplied, a grammar-valid candidate that crashes at runtime is fed back for
    repair; if it still crashes after ``max_retries`` it is replaced with the
    reject sentinel. Benchmark callers pass a mock-env probe; live deployments
    that lack a safe sim env leave it None.

    The cache key (controlled by the caller via ``cache_path``) should
    reflect ``model``, ``max_retries``, ``enable_judge``, and ``judge_model``
    so different configurations do not contaminate each other's results.
    """
    cached = _read_cached(cache_path, model) if cache_path else None
    if cached is not None:
        return cached

    if client is None:
        if not has_api_key():
            raise RuntimeError(
                "OPENAI_API_KEY is not set; cannot run agentic the Planner without a cache hit"
            )
        from openai import OpenAI  # lazy import

        client = OpenAI()

    if enable_judge and judge_client is None:
        judge_client = _get_judge_client(judge_model, client)

    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(task, tools)},
    ]

    total_prompt_tokens = 0
    total_completion_tokens = 0
    failure_history: list[str] = []
    judge_verdicts: list[JudgeVerdict] = []
    last_code = ""

    for attempt in range(1, max_retries + 2):  # initial + retries
        response = client.chat.completions.create(
            model=model,
            temperature=0,
            messages=messages,
        )
        code = _strip_fences(response.choices[0].message.content or "")
        usage = response.usage
        total_prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
        total_completion_tokens += getattr(usage, "completion_tokens", 0) or 0
        last_code = code

        # Stage 1: full grammar -- mirror pauth.pipeline.prepare so the loop
        # catches what the gateway's downstream prepare() would catch
        # (variable double-assignment, method calls, etc., are flagged by
        # validate_semantics, not parse_and_validate).
        tool_names = {t.name for t in tools}
        try:
            func = parse_and_validate(code)
            func = strip_dead_code(func, tool_names)
            validate_semantics(func, tool_names)
        except RestrictedGrammarError as exc:
            failure_history.append(f"grammar: {exc}")
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

        # Stage 1.5: deterministic one-sided prechecks. Cheaper and
        # stricter than the judge; runs first so mechanical over-authorization
        # never reaches a probabilistic verdict.
        precheck_issues = precheck_code(task, code, tools, policy=precheck_policy)
        if precheck_issues:
            failure_history.append(f"precheck: {'; '.join(precheck_issues)}")
            if attempt > max_retries:
                break
            messages.append({"role": "assistant", "content": code})
            messages.append(
                {
                    "role": "user",
                    "content": _PRECHECK_REPAIR_INSTRUCTION.format(
                        issues="\n".join(f"- {i}" for i in precheck_issues)
                    ),
                }
            )
            continue

        # Stage 1.75: runtime probe. Execute the grammar-valid code against a
        # throwaway mock environment to catch crashes (bad field / index / type
        # access) that static checks cannot see -- e.g. subscripting a text-blob
        # return, or comparing a datetime field to a string. Runs only when the
        # caller supplies an ``executor`` (the benchmark path, which has a mock
        # env at plan time); a live deployment would need a sandboxed sim env, so
        # production callers may leave it None. The probe never raises into the Planner.
        if executor is not None:
            try:
                runtime_error = executor(code)
            except Exception:  # noqa: BLE001 -- a probe failure must not crash the Planner
                runtime_error = None
            if runtime_error:
                failure_history.append(f"runtime: {runtime_error}")
                if attempt > max_retries:
                    break
                messages.append({"role": "assistant", "content": code})
                messages.append(
                    {
                        "role": "user",
                        "content": _RUNTIME_REPAIR_INSTRUCTION.format(error=runtime_error),
                    }
                )
                continue

        # Stage 2: semantic intent. Skipped only when explicitly disabled.
        if enable_judge:
            try:
                intent_ok, intent_issues = _judge_intent(
                    task, code, judge_model, judge_client
                )
            except Exception as exc:  # noqa: BLE001 -- judge failures shouldn't crash the Planner
                # Conservative fallback: treat judge-side errors as a failed
                # intent check so we don't accept unverified code.
                intent_ok = False
                intent_issues = [f"judge error: {type(exc).__name__}: {exc}"]
            # Record EVERY judge invocation, pass or fail. The visibility of
            # "judge was called, here is the verdict" matters even when the
            # verdict is PASS -- without it the reader cannot tell whether
            # the validator fired.
            judge_verdicts.append(
                JudgeVerdict(
                    attempt=attempt,
                    judge_model=judge_model,
                    intent_captured=intent_ok,
                    issues=list(intent_issues),
                )
            )
            if not intent_ok:
                issues_text = "; ".join(intent_issues) if intent_issues else "(no issues listed)"
                failure_history.append(f"intent: {issues_text}")
                if attempt > max_retries:
                    break
                messages.append({"role": "assistant", "content": code})
                messages.append(
                    {
                        "role": "user",
                        "content": _INTENT_REPAIR_INSTRUCTION.format(
                            issues="\n".join(f"- {i}" for i in intent_issues)
                            or "- (judge returned no specific issue list)"
                        ),
                    }
                )
                continue

        # Both stages passed.
        cost = _cost(model, total_prompt_tokens, total_completion_tokens)
        if cache_path is not None:
            _write_cache(
                cache_path, code, model, total_prompt_tokens, total_completion_tokens,
                cost, attempt, failure_history, judge_verdicts,
            )
        return AgenticCodegenResult(
            code=code,
            prompt_tokens=total_prompt_tokens,
            completion_tokens=total_completion_tokens,
            cost_usd=cost,
            cached=False,
            model=model,
            attempts=attempt,
            failure_history=failure_history,
            judge_verdicts=judge_verdicts,
        )

    # Retry budget exhausted -- last attempt failed grammar, precheck, or
    # intent. Downstream ``pauth.prepare`` rejects on grammar; a precheck- or
    # intent-only failure at the final round yields grammar-valid but unsafe
    # code, so we deliberately replace it with the explicit "do nothing"
    # sentinel that the gateway will reject by default-deny. This stops the
    # over-authorization accept where the gateway took a plan the validators
    # never passed.
    final_code = last_code
    if failure_history and failure_history[-1].startswith(("intent:", "precheck:", "runtime:")):
        final_code = "def run():\n    pass\n"
    cost = _cost(model, total_prompt_tokens, total_completion_tokens)
    if cache_path is not None:
        _write_cache(
            cache_path, final_code, model, total_prompt_tokens, total_completion_tokens,
            cost, max_retries + 1, failure_history, judge_verdicts,
        )
    return AgenticCodegenResult(
        code=final_code,
        prompt_tokens=total_prompt_tokens,
        completion_tokens=total_completion_tokens,
        cost_usd=cost,
        cached=False,
        model=model,
        attempts=max_retries + 1,
        failure_history=failure_history,
        judge_verdicts=judge_verdicts,
    )
