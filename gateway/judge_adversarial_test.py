"""Adversarial test of the Q15 semantic judge.

Feeds each hand-written intent-deficient case from
``tests/fixtures/ai_generated/l1_adversarial.py`` directly to
:func:`gateway.agentic_a1._judge_intent` and reports the verdict.

Pass criterion: every case is ruled ``intent_captured=False`` with at
least one non-empty issue. A judge that rubber-stamps will fail visibly
here; this is the evidence the validator actually fires.

Usage::

    .venv/bin/python gateway/judge_adversarial_test.py
    .venv/bin/python gateway/judge_adversarial_test.py --judge-model claude-opus-4-7
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway.agentic_a1 import (
    DEFAULT_JUDGE_MODEL,
    _get_anthropic_client,
    _judge_intent,
    load_me_env,
)
from tests.fixtures.ai_generated.l1_adversarial import ADVERSARIAL_CASES


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    parser.add_argument(
        "--only", default="",
        help="comma-separated case ids to run (default: all)",
    )
    args = parser.parse_args()

    load_me_env()
    client = _get_anthropic_client()

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    cases = [c for c in ADVERSARIAL_CASES if not only or c.id in only]

    print("=" * 78)
    print(
        f"adversarial judge test :: model={args.judge_model} "
        f"cases={len(cases)} (expecting EVERY case to be REJECTED)"
    )
    print("=" * 78)

    correctly_rejected = 0
    incorrectly_passed = 0
    failures: list[str] = []

    for c in cases:
        try:
            intent_ok, issues = _judge_intent(c.task, c.code, args.judge_model, client)
        except Exception as exc:  # noqa: BLE001
            print(f"\n[{c.id}] ERROR :: {type(exc).__name__}: {exc}")
            failures.append(f"{c.id}: judge raised {type(exc).__name__}")
            continue

        verdict = "PASS" if intent_ok else "FAIL"
        expected = "FAIL"  # we always expect the judge to fail these
        ok = (verdict == expected)
        if ok:
            correctly_rejected += 1
        else:
            incorrectly_passed += 1
            failures.append(f"{c.id} ({c.category}): judge said PASS but code is broken — {c.why_wrong}")

        print(f"\n[{c.id}] judge={verdict} (expected {expected}) :: {c.category}")
        print(f"  task: {c.task[:80]}...")
        print(f"  flaw: {c.why_wrong}")
        if issues:
            for i in issues:
                print(f"    issue: {i}")
        else:
            print("    issue: (judge produced no issue list)")

    print()
    print("=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print(f"cases:                 {len(cases)}")
    print(f"correctly rejected:    {correctly_rejected}")
    print(f"incorrectly passed:    {incorrectly_passed}")
    print(f"missed-detection rate: {incorrectly_passed / max(1, len(cases)) * 100:.1f}%")

    if failures:
        print("\nFAILURES")
        for f in failures:
            print(f"  - {f}")
        print("\nRESULT: JUDGE INSUFFICIENT — at least one bad code slipped through")
        return 1
    print("\nRESULT: JUDGE FIRED ON EVERY ADVERSARIAL CASE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
