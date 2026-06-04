"""End-to-end gateway experiment.

For each scripted scenario this runner:

  1. Submits the user prompt to the gateway. Plan generation happens here, once.
  2. Replays the simulated agent's tool-call attempts through the gateway.
  3. Asserts every gateway verdict matches the scenario's expected outcome.

A scenario fails if any verdict diverges. Aggregated, a PASS demonstrates that:

  * benign sequences along the recognised plan are permitted;
  * injection attempts -- off-slice operator, tampered constants, tampered
    derived operands, skipped observations, and quantity escalation -- are
    rejected;
  * rejected prompts cause default-deny on every subsequent call.

Run::

    .venv/bin/python -m run_gateway_experiment
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pauth.suites.base import SuiteSpec
from pauth.suites.shopping import build_suite as build_shopping_suite

# AgentDojo suites are optional: the gateway's invariants hold for any suite,
# but the demo scenarios live entirely on the self-contained shopping suite so
# the experiment runs without AgentDojo or an API key.
try:
    from tests.experiment.agentdojo_adapter import load_suite as load_agentdojo_suite
except Exception:  # noqa: BLE001 -- AgentDojo is optional for this demo
    load_agentdojo_suite = None  # type: ignore[assignment]

from gateway.gateway import Gateway
from tests.fixtures.l2_scenarios import SCENARIOS as CANONICAL_SCENARIOS

try:
    from tests.fixtures.ai_generated.l2_scenarios import AI_SCENARIOS
except Exception:  # noqa: BLE001 -- optional
    AI_SCENARIOS = []


def suite_loader(name: str) -> SuiteSpec:
    if name == "shopping":
        return build_shopping_suite()
    if load_agentdojo_suite is None:
        raise RuntimeError(
            f"suite {name!r} requires the AgentDojo adapter; install agentdojo or use shopping only"
        )
    return load_agentdojo_suite(name)


def _short(reason: str, width: int = 100) -> str:
    reason = reason.replace("\n", " ")
    if len(reason) <= width:
        return reason
    return reason[: width - 3] + "..."


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", choices=["canonical", "ai", "both"], default="canonical")
    args = parser.parse_args()

    if args.fixtures == "canonical":
        scenarios = list(CANONICAL_SCENARIOS)
    elif args.fixtures == "ai":
        scenarios = list(AI_SCENARIOS)
    else:
        scenarios = list(CANONICAL_SCENARIOS) + list(AI_SCENARIOS)

    print("=" * 78)
    print(f"gateway experiment :: fixtures={args.fixtures} ({len(scenarios)} scenarios)")
    print("=" * 78)

    failures: list[str] = []
    submissions_seen = 0
    submissions_correct = 0
    attempts_seen = 0
    attempts_correct = 0

    for scenario in scenarios:
        gateway = Gateway(suite_loader)
        submission = gateway.submit_user_prompt(scenario.prompt)
        submissions_seen += 1
        submission_ok = submission.accepted == scenario.submission_should_accept
        if submission_ok:
            submissions_correct += 1
        else:
            failures.append(
                f"{scenario.id}: submission accepted={submission.accepted}, "
                f"expected={scenario.submission_should_accept}"
            )

        verdict_label = "ACCEPT" if submission.accepted else "REJECT"
        expected_label = "ACCEPT" if scenario.submission_should_accept else "REJECT"
        print(f"\n[{scenario.id}]")
        print(
            f"  submission: {verdict_label} (expected {expected_label}) "
            f"rules={submission.rule_count} :: {_short(submission.reason)}"
        )

        for i, attempt in enumerate(scenario.attempts):
            result = gateway.handle_tool_call(attempt.tool, attempt.args)
            attempts_seen += 1
            ok = result.permit == attempt.expected_permit
            if ok:
                attempts_correct += 1
            else:
                failures.append(
                    f"{scenario.id}#{i}: {attempt.tool} permit={result.permit}, "
                    f"expected={attempt.expected_permit} :: {_short(result.reason)}"
                )
            v = "PERMIT" if result.permit else "REJECT"
            e = "PERMIT" if attempt.expected_permit else "REJECT"
            print(f"  [{i}] {v} (expected {e}) {attempt.tool}({_args(attempt.args)})")
            print(f"       label: {attempt.label}")
            print(f"       reason: {_short(result.reason)}")

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print(f"scenarios:   {len(scenarios)}")
    print(f"submissions: {submissions_correct}/{submissions_seen} matched expectation")
    print(f"attempts:    {attempts_correct}/{attempts_seen} matched expectation")
    print(f"failures:    {len(failures)}")

    if failures:
        print("\nFAILURES")
        for f in failures:
            print(f"  - {f}")
        print("\nRESULT: FAIL")
        return 1
    print("\nRESULT: PASS")
    return 0


def _args(args: list) -> str:
    parts = []
    for a in args:
        if isinstance(a, str) and len(a) > 28:
            parts.append(repr(a[:25] + "..."))
        else:
            parts.append(repr(a))
    return ", ".join(parts)


if __name__ == "__main__":
    raise SystemExit(main())
