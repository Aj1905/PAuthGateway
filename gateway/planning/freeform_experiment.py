"""Baseline measurement: how well does PAuth handle free-form prompts?

The deterministic recognizer is bypassed. The LLM A1 step generates code
directly from each prompt. We then measure:

  1. A1 success -- the generated code conforms to the restricted grammar
     and ``pauth.prepare`` succeeds (slice/rule derivation works).
  2. Tool coverage -- which tools the generated code calls vs. expected.
  3. Spurious calls -- tools that appear in the code but should not (the
     prompt-injection probe in particular).

This is intentionally cheap: each prompt is one A1 call. The runner caches
generated code under ``gateway/cache/freeform/`` so repeat runs are free.

Usage::

    .venv/bin/python gateway/freeform_experiment.py
    .venv/bin/python gateway/freeform_experiment.py --model gpt-4.1-mini
    .venv/bin/python gateway/freeform_experiment.py --no-cache  # re-run A1
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pauth.suites.shopping import build_suite as build_shopping_suite

from gateway.runtime.gateway import Gateway, SubmissionResult
from tests.fixtures.l1_prompts import FREEFORM_CASES as CANONICAL_FREEFORM, PromptCase

try:
    from tests.fixtures.ai_generated.l1_prompts import AI_FREEFORM_CASES
except Exception:  # noqa: BLE001 -- optional
    AI_FREEFORM_CASES = []


CACHE_DIR = ROOT / "gateway" / "cache" / "freeform"


def _load_env_file() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


@dataclasses.dataclass
class Outcome:
    fp: PromptCase
    submission: SubmissionResult
    code: str | None
    called_tools: list[str]
    missing_must: list[str]
    spurious_must_not: list[str]
    attempts: int
    failure_history: list[str]
    judge_verdicts: list[dict]


def _read_attempt_meta(cache_path: Path | None) -> tuple[int, list[str], list[dict]]:
    if cache_path is None or not cache_path.exists():
        return (1, [], [])
    meta_path = cache_path.with_suffix(".json")
    if not meta_path.exists():
        return (1, [], [])
    import json as _json
    try:
        meta = _json.loads(meta_path.read_text())
    except Exception:  # noqa: BLE001
        return (1, [], [])
    return (
        meta.get("attempts", 1),
        meta.get("failure_history", []),
        meta.get("judge_verdicts", []) or [],
    )


def _cache_path(
    prompt_id: str, model: str, max_retries: int,
    enable_judge: bool, judge_model: str, use_cache: bool,
) -> Path | None:
    if not use_cache:
        return None
    judge_tag = f"j{'1' if enable_judge else '0'}-{judge_model}"
    slug = hashlib.sha1(
        f"{model}::{prompt_id}::r{max_retries}::{judge_tag}".encode()
    ).hexdigest()[:12]
    return CACHE_DIR / f"{prompt_id}-{model}-r{max_retries}-{judge_tag}-{slug}.py"


def _tool_calls_in_code(code: str, tool_names: set[str]) -> list[str]:
    """Cheap extractor: tool name followed by ``(`` after a word boundary."""
    import re
    found: list[str] = []
    seen: set[str] = set()
    for name in tool_names:
        if re.search(rf"\b{re.escape(name)}\s*\(", code):
            if name not in seen:
                found.append(name)
                seen.add(name)
    return found


def measure(args: argparse.Namespace) -> int:
    _load_env_file()

    suite_factory = build_shopping_suite
    suite_name = "shopping"
    suite_tools = suite_factory().tool_names()

    def suite_loader(name: str):
        if name != suite_name:
            raise ValueError(f"freeform experiment is shopping-only, not {name!r}")
        return suite_factory()

    if args.fixtures == "canonical":
        cases = list(CANONICAL_FREEFORM)
    elif args.fixtures == "ai":
        cases = list(AI_FREEFORM_CASES)
    else:
        cases = list(CANONICAL_FREEFORM) + list(AI_FREEFORM_CASES)

    print("=" * 78)
    print(
        f"freeform A1 measurement :: fixtures={args.fixtures} "
        f"({len(cases)} prompts), suite={suite_name}, model={args.model}"
    )
    print("=" * 78)

    outcomes: list[Outcome] = []
    for fp in cases:
        gateway = Gateway(suite_loader)
        cache = _cache_path(
            fp.id, args.model, args.max_retries,
            enable_judge=not args.no_judge, judge_model=args.judge_model,
            use_cache=not args.no_cache,
        )
        submission = gateway.submit_user_prompt_freeform(
            fp.prompt, suite_name, model=args.model, cache_path=cache,
            max_retries=args.max_retries,
            enable_judge=not args.no_judge,
            judge_model=args.judge_model,
        )
        code = gateway.current_code()
        called = _tool_calls_in_code(code, suite_tools) if code else []
        missing = [t for t in fp.must_call if t not in called]
        spurious = [t for t in fp.must_not_call if t in called]

        # Read attempts / failure_history / judge verdicts from cache metadata.
        attempts, failure_history, judge_verdicts = _read_attempt_meta(cache)
        outcomes.append(Outcome(
            fp, submission, code, called, missing, spurious,
            attempts, failure_history, judge_verdicts,
        ))

        verdict = "ACCEPT" if submission.accepted else "REJECT"
        judge_summary = ""
        if judge_verdicts:
            pass_n = sum(1 for v in judge_verdicts if v.get("intent_captured"))
            fail_n = len(judge_verdicts) - pass_n
            judge_summary = f" judge={pass_n}P/{fail_n}F"
        print(
            f"\n[{fp.id}] {verdict} :: rules={submission.rule_count} "
            f"attempts={attempts}{judge_summary}"
        )
        print(f"  intent: {fp.note}")
        print(f"  reason: {submission.reason}")
        if called:
            print(f"  called: {', '.join(called)}")
        if missing:
            print(f"  MISSING must_call: {', '.join(missing)}")
        if spurious:
            print(f"  SPURIOUS must_not_call: {', '.join(spurious)}")
        if failure_history:
            print("  retry trace:")
            for i, err in enumerate(failure_history, 1):
                print(f"    [{i}] {err}")
        if judge_verdicts:
            print("  judge verdicts:")
            for v in judge_verdicts:
                vlabel = "PASS" if v.get("intent_captured") else "FAIL"
                issues_str = "; ".join(v.get("issues") or []) if not v.get("intent_captured") else ""
                model_lbl = v.get("judge_model", "")
                tail = f" :: {issues_str}" if issues_str else ""
                print(f"    attempt {v.get('attempt')} [{model_lbl}] {vlabel}{tail}")
        if args.show_code and code:
            print("  --- code ---")
            for line in code.splitlines():
                print(f"  {line}")

    accepted = [o for o in outcomes if o.submission.accepted]
    rejected = [o for o in outcomes if not o.submission.accepted]
    intent_mismatches = [o for o in accepted if o.missing_must or o.spurious_must_not]
    false_accepts = [o for o in outcomes if o.submission.accepted and not o.fp.expected_accept]
    false_rejects = [o for o in outcomes if not o.submission.accepted and o.fp.expected_accept]

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print(f"prompts:                  {len(outcomes)}")
    print(f"A1+A2/A3 accepted:        {len(accepted)} / {len(outcomes)}")
    print(f"rejected:                 {len(rejected)}")
    print(f"accepted but off-intent:  {len(intent_mismatches)}")
    print(f"false accepts:            {len(false_accepts)}")
    print(f"false rejects:            {len(false_rejects)}")

    if false_accepts:
        print("\nFALSE ACCEPTS (fixture expected reject, gateway accepted)")
        for o in false_accepts:
            print(f"  - {o.fp.id}")
    if false_rejects:
        print("\nFALSE REJECTS (fixture expected accept, gateway rejected)")
        for o in false_rejects:
            print(f"  - {o.fp.id}: {o.submission.reason}")

    if rejected:
        print("\nREJECTIONS")
        for o in rejected:
            print(f"  - {o.fp.id}: {o.submission.reason}")

    if intent_mismatches:
        print("\nOFF-INTENT (accepted but not what the prompt asked for)")
        for o in intent_mismatches:
            parts = []
            if o.missing_must:
                parts.append(f"missing={','.join(o.missing_must)}")
            if o.spurious_must_not:
                parts.append(f"spurious={','.join(o.spurious_must_not)}")
            print(f"  - {o.fp.id}: {' | '.join(parts)}")

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--model", default="gpt-4.1", help="OpenAI model id (default: gpt-4.1)")
    parser.add_argument("--no-cache", action="store_true", help="ignore cached A1 outputs")
    parser.add_argument("--show-code", action="store_true", help="print generated code per prompt")
    parser.add_argument(
        "--max-retries", type=int, default=3,
        help="grammar-repair retries; 0 disables the loop (paper-faithful one-shot)",
    )
    parser.add_argument(
        "--no-judge", action="store_true",
        help="disable the Q15 semantic judge (grammar check only)",
    )
    parser.add_argument(
        "--judge-model", default="claude-opus-4-8",
        help="Anthropic model id for the Q15 semantic judge (default: claude-opus-4-8)",
    )
    parser.add_argument(
        "--fixtures", choices=["canonical", "ai", "both"], default="canonical",
        help="which prompt set to run",
    )
    args = parser.parse_args(argv)
    return measure(args)


if __name__ == "__main__":
    raise SystemExit(main())
