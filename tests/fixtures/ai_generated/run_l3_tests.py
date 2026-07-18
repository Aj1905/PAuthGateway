"""Run AI-generated L3 reference cases through the full PAuth pipeline.

For each ``ReferenceCase`` in :data:`AI_REFERENCES`:

  1. ``pauth.prepare(reference_code)`` -- Slicer/Rule-compiler, rule derivation.
  2. Execute the reference code through the enforcer (benign run). Any
     denial here is a *false positive* (FP).
  3. Replay every ``forced_injections`` entry through the enforcer with the
     benign envelope store populated. Anything permitted is a
     *false negative* (FN).

This mirrors what ``tests/test_worked_examples.py`` does for the
hand-curated shopping tasks, but runs against the AI fixtures so the
reviewer can spot wrong injections (e.g. ones that happen to be on-slice
and would *correctly* be permitted -- those are bugs in the fixture, not
PAuth).

Usage::

    .venv/bin/python tests/fixtures/ai_generated/run_l3_tests.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pauth import (
    Enforcer,
    EnvelopeStore,
    KeyRing,
    check_injection,
    execute_generated_code,
    prepare,
)
from pauth.suites.shopping import build_suite as build_shopping_suite

from tests.fixtures.ai_generated.l3_references import AI_REFERENCES


def main() -> int:
    suites = {"shopping": build_shopping_suite()}

    print("=" * 78)
    print(f"L3 AI fixtures: benign FP check + forced-injection FN check ({len(AI_REFERENCES)} cases)")
    print("=" * 78)

    fp_total = 0
    fn_total = 0
    failures: list[str] = []

    for ref in AI_REFERENCES:
        suite = suites.get(ref.suite)
        if suite is None:
            failures.append(f"{ref.id}: unknown suite {ref.suite!r}")
            print(f"\n[{ref.id}] SKIP :: unknown suite")
            continue

        try:
            prepared = prepare(
                ref.reference_code, suite.tool_names(), suite.tool_signer()
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{ref.id}: prepare failed: {exc}")
            print(f"\n[{ref.id}] FAIL :: prepare failed: {exc}")
            continue

        env = suite.make_env()
        keyring = KeyRing()
        store = EnvelopeStore(keyring)
        enforcer = Enforcer(prepared.rules, store, suite.tool_signer())
        runner = suite.runner_factory(env)

        # Benign execution -- routes every call through the enforcer.
        report = execute_generated_code(
            ref.reference_code,
            enforcer,
            suite.tool_params(),
            runner,
            stop_on_denial=False,
        )
        fp_here = len(report.denied)
        fp_total += fp_here

        # Forced injections -- offer each one to the enforcer.
        permitted: list[str] = []
        for fc in ref.forced_injections:
            decision = check_injection(enforcer, fc.tool, fc.args)
            if decision.permit:
                permitted.append(f"{fc.tool}({fc.args}) [note: {fc.note}]")
        fn_here = len(permitted)
        fn_total += fn_here

        print(f"\n[{ref.id}]")
        print(f"  rules={len(prepared.rules)}")
        print(f"  benign calls={len(report.events)} FP(denied)={fp_here} crashed={report.crashed}")
        print(f"  injections={len(ref.forced_injections)} FN(permitted)={fn_here}")
        if report.denied:
            for ev in report.denied:
                print(f"    FP -- {ev.tool}({ev.args}) :: {ev.decision.reason}")
        if permitted:
            for p in permitted:
                print(f"    FN -- {p}")

    print("\n" + "=" * 78)
    print(f"summary: FP={fp_total} FN={fn_total} fixture_failures={len(failures)}")
    if failures:
        print("\nFIXTURE FAILURES")
        for f in failures:
            print(f"  - {f}")

    ok = fp_total == 0 and fn_total == 0 and not failures
    print(f"\nRESULT: {'PASS' if ok else 'REVIEW NEEDED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
