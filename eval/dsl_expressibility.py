"""Deterministic G1/G2 task-level expressibility case evaluation.

The evaluator deliberately avoids Planner output.  Each case fixes a task
meaning, an input domain, a canonical ``run()`` witness, and representative
regression states.  The states check this implementation; they are not a proof
over an unbounded input domain.

The two fan-out cases have no G1 witness for a structural reason, not because a
particular generated program was rejected: the schema exposes only one-element
mutation tools, every G1 program has a fixed finite number of tool-call sites,
and the task requires one call for every element of a runtime collection whose
valid length has no plan-time fixed bound.  G2 expresses the same requirement
with a bounded ``for`` over a recorded finite collection.  The script records
that analytic classification; it does not mechanically prove it.
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any

from pauth import prepare
from pauth.dsl_validator import DSLRejectionError
from pauth.enforcer import Enforcer, check_injection
from pauth.envelope import EnvelopeStore, KeyRing
from pauth.tool_executor import execute_generated_code


_TOOL_PARAMS = {
    "read_record": [],
    "read_items": [],
    "read_groups": [],
    "emit": ["target"],
    "emit_pair": ["group_id", "item_id"],
}
_TOOL_NAMES = set(_TOOL_PARAMS)
_TOOL_SIGNERS = {name: "expressibility-suite" for name in _TOOL_NAMES}


@dataclasses.dataclass
class _Environment:
    record: dict[str, Any]
    items: list[dict[str, Any]]
    groups: list[dict[str, Any]]
    emitted: list[str] = dataclasses.field(default_factory=list)
    emitted_pairs: list[tuple[str, str]] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True)
class _State:
    state_id: str
    record: dict[str, Any] = dataclasses.field(default_factory=dict)
    items: tuple[dict[str, Any], ...] = ()
    groups: tuple[dict[str, Any], ...] = ()
    expected_emitted: tuple[str, ...] = ()
    expected_pairs: tuple[tuple[str, str], ...] = ()
    invalid_probe: tuple[str, tuple[Any, ...]] | None = None


@dataclasses.dataclass(frozen=True)
class _Case:
    case_id: str
    requirement: str
    domain: str
    code: str
    states: tuple[_State, ...]
    g1_expressible: bool
    g1_argument: str
    g2_argument: str


_FIXED_CALL = '''def run():
    emit("fixed")
'''

_READ_THEN_CALL = '''def run():
    record = read_record()
    emit(record.target)
'''

_FLAT_GUARD = '''def run():
    record = read_record()
    if record.count > 0:
        emit(record.target)
'''

_HELPER_SELECTION = '''def run():
    items = read_items()
    selected = first(items, predicate=lambda item: item.kind == "chosen")
    emit(selected.id)
'''

_SINGLE_FANOUT = '''def run():
    items = read_items()
    for item in items:
        emit(item.id)
'''

_DEPENDENT_FANOUT = '''def run():
    groups = read_groups()
    for group in groups:
        for item in group.members:
            emit_pair(group.id, item.id)
'''


def _item(item_id: str, kind: str = "other") -> dict[str, str]:
    return {"id": item_id, "kind": kind}


CASES: tuple[_Case, ...] = (
    _Case(
        "fixed_call",
        "fixed tool call",
        "The single fixed task state.",
        _FIXED_CALL,
        (_State("fixed", expected_emitted=("fixed",)),),
        True,
        "A fixed call is a G1 statement.",
        "The witness contains one fixed call and is independent of runtime data.",
    ),
    _Case(
        "read_then_call",
        "call using a field from a recorded tool result",
        "Records containing a target field.",
        _READ_THEN_CALL,
        (
            _State("target-a", record={"target": "a"}, expected_emitted=("a",)),
            _State("target-b", record={"target": "b"}, expected_emitted=("b",)),
        ),
        True,
        "G1 can bind one tool result and use one of its fields.",
        "The witness binds the returned record and passes its target field.",
    ),
    _Case(
        "flat_guard",
        "flat conditional call",
        "Records with a numeric count and a target field.",
        _FLAT_GUARD,
        (
            _State("guard-false", record={"count": 0, "target": "a"}),
            _State(
                "guard-true",
                record={"count": 2, "target": "b"},
                expected_emitted=("b",),
            ),
        ),
        True,
        "A flat if statement is part of G1.",
        "The witness covers both outcomes of the predicate over the defined records.",
    ),
    _Case(
        "helper_selection",
        "single-element selection with an Appendix A helper",
        "Finite item lists containing at least one item whose kind is chosen.",
        _HELPER_SELECTION,
        (
            _State(
                "chosen-first",
                items=(_item("a", "chosen"), _item("b")),
                expected_emitted=("a",),
            ),
            _State(
                "chosen-middle",
                items=(_item("a"), _item("b", "chosen"), _item("c")),
                expected_emitted=("b",),
            ),
            _State(
                "chosen-last",
                items=(_item("a"), _item("b"), _item("c", "chosen")),
                expected_emitted=("c",),
            ),
        ),
        True,
        "G1 includes first(values, predicate=lambda value: ...).",
        "The helper returns the first matching element of every list in the domain.",
    ),
    _Case(
        "single_fanout",
        "one call per element of a runtime collection",
        "All finite item lists returned at runtime, with no plan-time fixed "
        "length bound; the schema exposes emit(target), not a bulk mutation tool.",
        _SINGLE_FANOUT,
        tuple(
            _State(
                f"size-{size}",
                items=tuple(_item(f"item-{index}") for index in range(size)),
                expected_emitted=tuple(f"item-{index}" for index in range(size)),
                invalid_probe=("emit", ("outside",)),
            )
            for size in (0, 1, 4, 16)
        ),
        False,
        "G1 has no loop or recursion, so one program cannot issue one call for "
        "every element of runtime collections with arbitrary finite length.",
        "The bounded for visits every element of any finite returned item list once.",
    ),
    _Case(
        "dependent_nested_fanout",
        "one call per reachable parent-child pair",
        "All finite group lists with finite member lists returned at runtime, "
        "with no plan-time fixed length bound; the schema exposes "
        "emit_pair(group_id, item_id), not a bulk mutation tool.",
        _DEPENDENT_FANOUT,
        (
            _State(
                "empty",
                invalid_probe=("emit_pair", ("outside-group", "outside-item")),
            ),
            _State(
                "one-pair",
                groups=({"id": "g1", "members": [_item("a")]},),
                expected_pairs=(("g1", "a"),),
                invalid_probe=("emit_pair", ("g1", "outside-item")),
            ),
            _State(
                "dependent-pairs",
                groups=(
                    {"id": "g1", "members": [_item("a"), _item("b")]},
                    {"id": "g2", "members": [_item("c")]},
                ),
                expected_pairs=(("g1", "a"), ("g1", "b"), ("g2", "c")),
                invalid_probe=("emit_pair", ("g1", "c")),
            ),
        ),
        False,
        "The required call count follows runtime parent-child collections and "
        "has no fixed bound in G1.",
        "The nested bounded for visits every reachable parent-child pair in any "
        "finite returned hierarchy once.",
    ),
)


def _make_environment(state: _State) -> _Environment:
    return _Environment(
        record=copy.deepcopy(state.record),
        items=copy.deepcopy(list(state.items)),
        groups=copy.deepcopy(list(state.groups)),
    )


def _tool_executor(environment: _Environment):
    def execute(tool: str, kwargs: dict[str, Any]) -> Any:
        if tool == "read_record":
            return environment.record
        if tool == "read_items":
            return environment.items
        if tool == "read_groups":
            return environment.groups
        if tool == "emit":
            environment.emitted.append(kwargs["target"])
            return {"ok": True}
        if tool == "emit_pair":
            pair = (kwargs["group_id"], kwargs["item_id"])
            environment.emitted_pairs.append(pair)
            return {"ok": True}
        raise KeyError(tool)

    return execute


def _run_state(code: str, profile: str, state: _State) -> dict[str, Any]:
    prepared = prepare(code, _TOOL_NAMES, _TOOL_SIGNERS, dsl_profile=profile)
    enforcer = Enforcer(
        prepared.rules,
        EnvelopeStore(KeyRing()),
        _TOOL_SIGNERS,
    )
    environment = _make_environment(state)
    report = execute_generated_code(
        prepared.source,
        enforcer,
        _TOOL_PARAMS,
        _tool_executor(environment),
    )
    expected_emitted = list(state.expected_emitted)
    expected_pairs = [tuple(pair) for pair in state.expected_pairs]
    trace_ok = (
        report.crashed is None
        and not report.denied
        and not report.tool_errors
        and environment.emitted == expected_emitted
        and environment.emitted_pairs == expected_pairs
    )
    probe_denied: bool | None = None
    if state.invalid_probe is not None:
        tool, args = state.invalid_probe
        probe_denied = not check_injection(enforcer, tool, list(args)).permit
    return {
        "state_id": state.state_id,
        "trace_ok": trace_ok,
        "crashed": report.crashed,
        "denials": len(report.denied),
        "tool_errors": list(report.tool_errors),
        "observed_emitted": list(environment.emitted),
        "observed_pairs": [list(pair) for pair in environment.emitted_pairs],
        "invalid_probe_denied": probe_denied,
    }


def evaluate() -> dict[str, Any]:
    case_rows: list[dict[str, Any]] = []
    totals = {
        "g1": {"passed_cases": 0, "total_cases": len(CASES)},
        "g2": {"passed_cases": 0, "total_cases": len(CASES)},
    }
    denied_probes = total_probes = 0

    for case in CASES:
        profiles: dict[str, Any] = {}
        for profile in ("g1", "g2"):
            if profile == "g1" and not case.g1_expressible:
                rejected = False
                rejection = ""
                try:
                    prepare(
                        case.code,
                        _TOOL_NAMES,
                        _TOOL_SIGNERS,
                        dsl_profile="g1",
                    )
                except DSLRejectionError as exc:
                    rejected = True
                    rejection = str(exc)
                if not rejected:
                    raise AssertionError(
                        f"analytic G1 classification is stale for {case.case_id}: "
                        "the canonical bounded-for witness is no longer rejected"
                    )
                profiles[profile] = {
                    "expressible": False,
                    "classification_basis": "analytic_nonexpressibility",
                    "witness_rejected": rejected,
                    "rejection": rejection,
                    "argument": case.g1_argument,
                    "representative_states": [],
                }
                continue

            states = [_run_state(case.code, profile, state) for state in case.states]
            expressible = all(row["trace_ok"] for row in states)
            profiles[profile] = {
                "expressible": expressible,
                "classification_basis": "canonical_witness_and_domain_argument",
                "argument": case.g1_argument if profile == "g1" else case.g2_argument,
                "representative_states": states,
            }
            if expressible:
                totals[profile]["passed_cases"] += 1
            if profile == "g2":
                for row in states:
                    verdict = row["invalid_probe_denied"]
                    if verdict is not None:
                        total_probes += 1
                        denied_probes += int(verdict)

        case_rows.append(
            {
                "case_id": case.case_id,
                "requirement": case.requirement,
                "domain": case.domain,
                "source_sha256": hashlib.sha256(case.code.encode("utf-8")).hexdigest(),
                "profiles": profiles,
            }
        )

    for profile in totals:
        passed = totals[profile]["passed_cases"]
        total = totals[profile]["total_cases"]
        totals[profile]["rate"] = passed / total
    delta = 100 * (totals["g2"]["rate"] - totals["g1"]["rate"])
    return {
        "schema_version": 2,
        "metric": "FEASIBILITY_EXPRESSIBLE",
        "method": (
            "Cases define semantic input domains. Positive classifications use a "
            "profile-valid canonical run() witness plus an argument that it applies "
            "to the domain. G1-negative fan-out cases use a finite-call-site proof "
            "under a one-element mutation-tool schema. Representative states are "
            "implementation regression checks, not proofs over unbounded domains."
        ),
        "totals": totals,
        "delta_percentage_points": delta,
        "loop_invariant_checks": {
            "invalid_probes_denied": denied_probes,
            "total_invalid_probes": total_probes,
        },
        "cases": case_rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = evaluate()
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
