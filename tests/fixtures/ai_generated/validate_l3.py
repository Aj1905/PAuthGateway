"""Smoke-validate AI-generated L3 reference codes.

For every entry in :data:`AI_REFERENCES`, attempt ``pauth.prepare``:

* ``OK`` -- the ``reference_code`` parses, conforms to the restricted
  grammar, and produces a non-empty rule set. (It does not prove the
  rules match the prompt's intent -- a human still has to read them.)
* ``FAIL`` -- print the exception so the reviewer can fix or discard.

Usage::

    .venv/bin/python tests/fixtures/ai_generated/validate_l3.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pauth import prepare
from pauth.suites.shopping import build_suite as build_shopping_suite

from tests.fixtures.ai_generated.l3_references import AI_REFERENCES


def main() -> int:
    suites = {
        "shopping": build_shopping_suite(),
    }

    print("=" * 78)
    print(f"L3 smoke-validation: {len(AI_REFERENCES)} AI-generated reference case(s)")
    print("=" * 78)

    ok = 0
    failed: list[tuple[str, str]] = []
    for ref in AI_REFERENCES:
        suite = suites.get(ref.suite)
        if suite is None:
            failed.append((ref.id, f"unknown suite {ref.suite!r}"))
            print(f"\n[{ref.id}] FAIL :: unknown suite {ref.suite!r}")
            continue
        try:
            prepared = prepare(
                ref.reference_code, suite.tool_names(), suite.tool_signer()
            )
        except Exception as exc:  # noqa: BLE001
            failed.append((ref.id, f"{type(exc).__name__}: {exc}"))
            print(f"\n[{ref.id}] FAIL :: {type(exc).__name__}: {exc}")
            continue
        ok += 1
        print(
            f"\n[{ref.id}] OK :: {len(prepared.rules)} rule(s), "
            f"{len(prepared.slices)} slice(s), "
            f"{len(ref.forced_injections)} forced injection(s) recorded"
        )

    print("\n" + "=" * 78)
    print(f"summary: {ok} OK, {len(failed)} FAIL")
    if failed:
        print("\nFAILURES")
        for case_id, reason in failed:
            print(f"  - {case_id}: {reason}")
        print("\nRESULT: REVIEW NEEDED")
        return 1
    print("\nRESULT: all reference codes compile")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
