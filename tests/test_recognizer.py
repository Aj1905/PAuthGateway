from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pauth import prepare
from pauth.suites.shopping import build_suite as build_shopping_suite
from benchmarks.agentdojo_adapter import load_suite

from gateway.planning.core import (
    CaseResult,
    build_llm_translator,
    canonical_json,
    fixture_translate,
    mutate_run,
    run_to_pauth_code,
    translate_with_gate,
    verify_run,
)
from tests.fixtures.l1_prompts import RECOGNIZER_CASES as CANONICAL_RECOGNIZER

try:
    from tests.fixtures.ai_generated.l1_prompts import AI_RECOGNIZER_CASES
except Exception:  # noqa: BLE001
    AI_RECOGNIZER_CASES = []


def _prepare_with_pauth(run_doc: dict) -> tuple[bool, str]:
    code = run_to_pauth_code(run_doc)
    suite_name = run_doc["suite"]
    try:
        suite = build_shopping_suite() if suite_name == "shopping" else load_suite(suite_name)
        prepared = prepare(code, suite.tool_names(), suite.tool_signer())
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}\n{code}"
    return True, f"{len(prepared.slices)} slice(s)"


def run(args: argparse.Namespace) -> int:
    if args.backend == "fixture":
        translator = fixture_translate
    else:
        translator = build_llm_translator(args.model, ROOT, args.temperature)

    results: list[CaseResult] = []
    false_accepts = 0
    false_rejects = 0
    mutation_failures: list[str] = []

    if args.fixtures == "canonical":
        cases = list(CANONICAL_RECOGNIZER)
    elif args.fixtures == "ai":
        cases = list(AI_RECOGNIZER_CASES)
    else:
        cases = list(CANONICAL_RECOGNIZER) + list(AI_RECOGNIZER_CASES)

    print("=" * 78)
    print(f"run() gate experiment :: fixtures={args.fixtures} ({len(cases)} cases)")
    print("=" * 78)
    print(f"backend={args.backend} retry_cap={args.retry_cap}")

    for case in cases:
        attempts, accepted_run = translate_with_gate(
            case.prompt,
            translator,
            args.retry_cap,
        )
        accepted = accepted_run is not None
        if accepted and not case.expected_accept:
            false_accepts += 1
        if not accepted and case.expected_accept:
            false_rejects += 1

        pauth_ok = False
        pauth_detail = "not accepted"
        if accepted_run is not None:
            pauth_ok, pauth_detail = _prepare_with_pauth(accepted_run)
            for mutant_name, mutant_raw in mutate_run(accepted_run):
                verdict = verify_run(case.prompt, mutant_raw)
                if verdict.ok:
                    mutation_failures.append(f"{case.id}: accepted mutant {mutant_name}")

        results.append(
            CaseResult(
                case=case,
                attempts=attempts,
                accepted=accepted,
                accepted_run=accepted_run,
                pauth_prepare_ok=pauth_ok,
                pauth_prepare_detail=pauth_detail,
            )
        )

        status = "ACCEPT" if accepted else "REJECT"
        expectation = "expected ACCEPT" if case.expected_accept else "expected REJECT"
        last = attempts[-1].verification
        print(f"\n[{case.id}] {status} ({expectation})")
        print(f"  note: {case.note}")
        print(f"  attempts: {len(attempts)}")
        print(f"  final reason: {last.reason}")
        print(f"  pauth.prepare: {pauth_detail}")
        if args.show_run and accepted_run is not None:
            print(f"  canonical run(): {canonical_json(accepted_run)}")

    accepted_count = sum(1 for r in results if r.accepted)
    rejected_count = len(results) - accepted_count
    pauth_failures = [r for r in results if r.accepted and not r.pauth_prepare_ok]

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print(f"cases: {len(results)}")
    print(f"accepted: {accepted_count}")
    print(f"rejected: {rejected_count}")
    print(f"false accepts: {false_accepts}")
    print(f"false rejects after retry budget: {false_rejects}")
    print(f"accepted-run() mutation failures: {len(mutation_failures)}")
    print(f"PAuth prepare failures for accepted run(): {len(pauth_failures)}")

    if mutation_failures:
        print("\nMUTATION FAILURES")
        for failure in mutation_failures:
            print(f"  - {failure}")

    if pauth_failures:
        print("\nPAUTH PREPARE FAILURES")
        for result in pauth_failures:
            print(f"  - {result.case.id}: {result.pauth_prepare_detail}")

    ok = (
        false_accepts == 0
        and not mutation_failures
        and not pauth_failures
        and (args.allow_false_rejects or false_rejects == 0)
    )
    if ok:
        print("\nRESULT: PASS")
        return 0
    print("\nRESULT: FAIL")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the conservative run() gate experiment.")
    parser.add_argument("--backend", choices=["fixture", "llm"], default="fixture")
    parser.add_argument("--model", default="gpt-4.1-mini")
    parser.add_argument(
        "--retry-cap",
        type=int,
        default=10,
        help="safety cap for retry-until-OK generation; prevents infinite loops",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.2,
        help="LLM sampling temperature; same-prompt retries are useless at strict determinism",
    )
    parser.add_argument("--show-run", action="store_true")
    parser.add_argument(
        "--fixtures", choices=["canonical", "ai", "both"], default="canonical",
        help="which RECOGNIZER case set to run",
    )
    parser.add_argument(
        "--allow-false-rejects",
        action="store_true",
        help="permit supported prompts to remain rejected after the retry budget",
    )
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
