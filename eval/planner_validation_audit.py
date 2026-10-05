"""Compare the repair loop's historical validation with downstream preparation.

Offline diagnostic over saved final candidates, not a new generation experiment.
Run: python -m eval.planner_validation_audit --output /tmp/validation-audit.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from pauth import prepare
from pauth.grammar_validator import (
    DSLRejectionError, parse_and_validate, strip_dead_code, validate_semantics,
)


def compare_validation(code: str, tool_names: set[str], signers: dict) -> dict:
    """Keep the historical path frozen so the comparison survives a repair."""
    result = {"historical_error": None, "downstream_error": None}
    try:
        func = strip_dead_code(parse_and_validate(code), tool_names)
        validate_semantics(func, tool_names)
    except DSLRejectionError as exc:
        result["historical_error"] = str(exc)
    try:
        prepare(code, tool_names, signers)
    except DSLRejectionError as exc:
        result["downstream_error"] = str(exc)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("tests/experiment/funnel_scratch"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    from benchmarks.agentdojo_adapter import load_suite
    from benchmarks.structured_read import augment_with_structuring

    suites = {n: augment_with_structuring(load_suite(n))
              for n in ("banking", "slack", "travel", "workspace")}
    counts, groups, differences = Counter(), {}, []
    digest = hashlib.sha256()
    for path in sorted(args.root.glob("*agentdojo_*/*/cand*.py")):
        group = path.parents[1].name
        suite = suites.get(group.rsplit("_", 1)[-1])
        if suite is None:
            continue
        code = path.read_text()
        sha = hashlib.sha256(code.encode()).hexdigest()
        relative = str(path.relative_to(args.root))
        digest.update(f"{relative}\0{sha}\n".encode())
        result = compare_validation(code, suite.tool_names(), suite.tool_signer())
        old, new = result["historical_error"], result["downstream_error"]
        group_counts = groups.setdefault(group, Counter())
        for counter in (counts, group_counts):
            counter["candidates"] += 1
            counter["historical_rejected"] += old is not None
            counter["downstream_rejected"] += new is not None
            counter["recoverable"] += old is not None and new is None
            counter["downstream_only_rejected"] += old is None and new is not None
        if (old is None) != (new is None):
            differences.append({"path": relative, "sha256": sha, **result})
    if not counts["candidates"]:
        parser.error("no saved candidates found; no measurement produced")
    output = {
        "schema": "planner_validation_audit_v1", "corpus_sha256": digest.hexdigest(),
        "scope": "Saved final candidates, not independent tasks or new LLM samples. "
                 "Acceptance does not establish task completion or least privilege.",
        "counts": counts, "groups": groups, "differences": differences,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps(counts, sort_keys=True))


if __name__ == "__main__":
    main()
