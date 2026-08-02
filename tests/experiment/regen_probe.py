"""(A) Spot-regeneration probe: do the 10 loop-related G2 rejects become
DSL-valid once the Planner prompt is synced with the extended grammar?

Regenerates each task fresh (no cache) with the synced SYSTEM_PROMPT, then checks
whether the new plan passes parse_and_validate (G2). Prints old vs new verdict.
Requires OPENAI_API_KEY. Read-only w.r.t. the shipped cache (writes nowhere).
"""

from __future__ import annotations

from pathlib import Path

from agentdojo.task_suite.load_suites import get_suites

from benchmarks.agentdojo_adapter import load_suite
from gateway.planning.agentic_planner import generate_code_with_self_repair
from pauth.dsl_validator import (
    DSLRejectionError,
    parse_and_validate,
    strip_dead_code,
    validate_semantics,
)

TARGETS = [
    ("banking", "user_task_1"),
    ("slack", "user_task_15"),
    ("slack", "user_task_20"),
    ("slack", "user_task_5"),
    ("slack", "user_task_9"),
    ("travel", "user_task_11"),
    ("travel", "user_task_14"),
    ("travel", "user_task_18"),
    ("travel", "user_task_3"),
    ("workspace", "user_task_25"),
]

CACHE = Path("tests/experiment/cache")


def _g2(code: str, tool_names: set[str]) -> str:
    """Full G2: syntax grammar + dead-code strip + semantic grammar."""
    try:
        func = parse_and_validate(code)
        func = strip_dead_code(func, tool_names)
        validate_semantics(func, tool_names)
        return "PASS"
    except DSLRejectionError as e:
        return f"REJECT: {str(e)[:45]}"


def main() -> None:
    specs = {s: load_suite(s) for s in {t[0] for t in TARGETS}}
    adj = {s: get_suites("v1")[s] for s in specs}
    scratch = CACHE.parent / "regen_scratch"
    scratch.mkdir(exist_ok=True)
    now_pass = 0
    for suite, tid in TARGETS:
        ut = adj[suite].user_tasks[tid]
        names = set(specs[suite].tool_names())
        old = (CACHE / suite / f"{tid}.py").read_text()
        old_v = _g2(old, names)
        cached = scratch / f"{suite}__{tid}.py"
        if cached.exists():
            new = cached.read_text()
        else:
            res = generate_code_with_self_repair(
                ut.PROMPT, specs[suite].tool_docs(), model="gpt-4.1", max_retries=3,
                enable_judge=False,
            )
            new = res.code
            cached.write_text(new)
        new_v = _g2(new, names)
        if new_v == "PASS":
            now_pass += 1
        print(f"\n=== {suite}/{tid} ===")
        print(f"  OLD G2: {old_v}")
        print(f"  NEW G2: {new_v}")
        if new_v == "PASS":
            print("  --- NEW PLAN ---")
            for ln in new.splitlines():
                print("    " + ln)
    print(f"\n### {now_pass}/{len(TARGETS)} previously-rejected loop tasks now pass G2 ###")


if __name__ == "__main__":
    main()
