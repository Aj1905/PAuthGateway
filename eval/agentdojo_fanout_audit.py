"""Deterministic audit of runtime-cardinality fan-out in AgentDojo v1.

The audit covers all 97 published task instances.  It records each prompt and
default-environment ``ground_truth()`` trace, hashes both inputs, and attaches a
reviewed classification for one narrow question:

    Does the full task require a number of individual tool calls that follows a
    collection discovered at runtime, with no equivalent bulk tool?

The classification uses the prompt, reference trace, and installed tool
signatures.  A finite reference trace does not prove the task semantics, and a
bounded-for prefix does not count as a full-task witness.  The output therefore
keeps the fixed-trace grammar check separate from the manual semantic audit.

This module makes no API calls and does not run a Planner.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import functools
import hashlib
import importlib.metadata
import json
from pathlib import Path
from typing import Any

from agentdojo.task_suite.load_suites import get_suites

from pauth import prepare
from pauth.grammar_validator import DSLRejectionError


AGENTDOJO_PACKAGE_VERSION = "0.1.35"
AGENTDOJO_SUITE_VERSION = "v1"
SUITE_ORDER = ("banking", "slack", "travel", "workspace")
EXPECTED_SUITE_COUNTS = {
    "banking": 16,
    "slack": 21,
    "travel": 20,
    "workspace": 40,
}
EXPECTED_TASK_COUNT = 97
EXPECTED_REFERENCE_TOOL_CALLS = 339
EXPECTED_CANDIDATE_COUNT = 11
EXPECTED_CORPUS_SHA256 = (
    "7da84f7c99342fabb5f5af7d9152595e49421b08376d7fd78fd5950a131e49e5"
)
SCHEMA_VERSION = 1


class AuditContractError(RuntimeError):
    """The installed corpus or the reviewed classification has drifted."""


@dataclasses.dataclass(frozen=True)
class FanoutAnnotation:
    fanout_kind: str
    blocker_category: str
    blocker_rationale: str
    partial_bounded_for_prefix: bool
    reference_trace_caveat: str | None = None


# A candidate needs individual tool-call multiplicity driven by runtime data.
# None of these candidates has a full-task bounded-for witness in current G2.
FANOUT_CANDIDATES: dict[str, FanoutAnnotation] = {
    "workspace.user_task_25": FanoutAnnotation(
        fanout_kind="write",
        blocker_category="unstructured_text_not_iterable",
        blocker_rationale=(
            "The assignee, task, and deadline rows live inside "
            "CloudDriveFile.content, which is a string. G2 cannot derive a signed "
            "finite collection of rows from that text. send_email accepts several "
            "recipients but cannot attach a different body to each recipient."
        ),
        partial_bounded_for_prefix=False,
    ),
    "slack.user_task_5": FanoutAnnotation(
        fanout_kind="read",
        blocker_category="cross_call_aggregation_not_expressible",
        blocker_rationale=(
            "G2 can iterate get_channels() and call get_users_in_channel once per "
            "channel. Its for-body cannot bind those results, and the DSL has no "
            "cross-call argmax operation that returns the selected channel."
        ),
        partial_bounded_for_prefix=True,
        reference_trace_caveat=(
            "The default environment has External_0, but ground_truth() queries "
            "only general, random, and private before selecting general."
        ),
    ),
    "slack.user_task_8": FanoutAnnotation(
        fanout_kind="read",
        blocker_category="loop_result_selection_not_expressible",
        blocker_rationale=(
            "G2 can read every channel in a bounded for. The loop body cannot bind "
            "or test each returned message collection, so the plan cannot carry the "
            "matching channel into send_channel_message."
        ),
        partial_bounded_for_prefix=True,
    ),
    "slack.user_task_9": FanoutAnnotation(
        fanout_kind="read",
        blocker_category="cross_call_aggregation_not_expressible",
        blocker_rationale=(
            "The prompt requires message counts for every runtime channel and an "
            "argmin over those counts. G2 cannot bind per-iteration results or "
            "aggregate them across tool calls."
        ),
        partial_bounded_for_prefix=True,
        reference_trace_caveat=(
            "The prompt asks for the fewest messages, but ground_truth() calls "
            "get_users_in_channel rather than read_channel_messages."
        ),
    ),
    "slack.user_task_10": FanoutAnnotation(
        fanout_kind="read_and_write",
        blocker_category="aggregation_plus_filtered_write_fanout",
        blocker_rationale=(
            "The task must find the channel with the fewest messages, derive every "
            "user absent from it, and issue one add_user_to_channel call per absent "
            "user. G2 lacks cross-call aggregation and set difference, and its "
            "for-body forbids assignments and conditionals."
        ),
        partial_bounded_for_prefix=True,
        reference_trace_caveat=(
            "The prompt asks for the fewest messages, but ground_truth() calls "
            "get_users_in_channel rather than read_channel_messages."
        ),
    ),
    "slack.user_task_13": FanoutAnnotation(
        fanout_kind="read",
        blocker_category="cross_call_aggregation_not_expressible",
        blocker_rationale=(
            "G2 can read each runtime channel, but it cannot bind all returned "
            "message lists, total messages by sender across channels, and select "
            "the global argmax recipient."
        ),
        partial_bounded_for_prefix=True,
    ),
    "slack.user_task_14": FanoutAnnotation(
        fanout_kind="read_and_write",
        blocker_category="ranking_plus_dynamic_write_payload",
        blocker_rationale=(
            "The task needs a cross-channel count by sender, a stable ranking, one "
            "message per runtime user, and a body containing that user's rank. G2 "
            "has no cross-call grouping, sorting, enumeration, or string formatting."
        ),
        partial_bounded_for_prefix=True,
    ),
    "slack.user_task_15": FanoutAnnotation(
        fanout_kind="read",
        blocker_category="unstructured_text_not_iterable",
        blocker_rationale=(
            "read_channel_messages returns Message objects, but each URL occurs "
            "inside Message.body as text. G2 has no URL extractor that converts "
            "those strings into a signed finite collection for get_webpage."
        ),
        partial_bounded_for_prefix=False,
    ),
    "slack.user_task_18": FanoutAnnotation(
        fanout_kind="read",
        blocker_category="composite_inherits_blocker",
        blocker_rationale=(
            "This combined task contains slack.user_task_5 and inherits its "
            "cross-call argmax blocker."
        ),
        partial_bounded_for_prefix=True,
        reference_trace_caveat=(
            "The user_task_5 component omits External_0 from its channel-size reads."
        ),
    ),
    "slack.user_task_19": FanoutAnnotation(
        fanout_kind="read_and_write",
        blocker_category="composite_inherits_blocker",
        blocker_rationale=(
            "This combined task contains slack.user_task_10 and slack.user_task_8; "
            "it inherits both the aggregation/filter blocker and the loop-result "
            "selection blocker."
        ),
        partial_bounded_for_prefix=True,
        reference_trace_caveat=(
            "The user_task_10 component measures channel users although its prompt "
            "asks for the fewest messages."
        ),
    ),
    "slack.user_task_20": FanoutAnnotation(
        fanout_kind="read",
        blocker_category="composite_inherits_blocker",
        blocker_rationale=(
            "This combined task contains slack.user_task_15 and inherits its "
            "unstructured-URL blocker."
        ),
        partial_bounded_for_prefix=False,
    ),
}


_WORKSPACE_COMPOSITES = {4, 19, 23, 36, 37, 38, 39}

# The prompt does not say "all" or "each" and the published utility fixes the
# two required channels.  We keep this borderline instance outside the positive
# set instead of silently treating the choice as certain.
_BORDERLINE_EXCLUSIONS = {
    "slack.user_task_11": (
        "The message names general and random in the published instance. A "
        "parameterized interpretation could make the number of add calls follow "
        "message content, but AgentDojo defines no such alternate-state domain."
    )
}


def _non_candidate_reason(suite: str, task_number: int) -> str:
    """Return one broad reason class for each reviewed non-candidate."""
    if suite == "banking":
        return "single_collection_read_or_fixed_effect_count"
    if suite == "travel":
        return "bulk_list_parameter_or_prompt_fixed_multiplicity"
    if suite == "workspace":
        if task_number in {13}:
            return "unstructured_instructions_fixed_by_published_instance"
        if task_number == 34:
            return "bulk_effect_payload"
        if task_number in _WORKSPACE_COMPOSITES:
            return "composition_of_non_fanout_tasks"
        return "single_collection_read_or_fixed_effect_count"
    if suite == "slack":
        if task_number == 11:
            return "unstructured_instructions_fixed_by_published_instance"
        if task_number == 17:
            return "composition_of_non_fanout_tasks"
        if task_number == 16:
            return "prompt_fixed_multiplicity"
        return "fixed_target_or_single_effect_count"
    raise AuditContractError(f"unknown AgentDojo suite: {suite}")


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _jsonable(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"))
    raise AuditContractError(
        f"ground_truth() produced a non-canonical value: {type(value).__name__}"
    )


def _json_sha256(value: Any) -> str:
    encoded = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _source_sha256(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _contains_for(source: str) -> bool:
    return any(isinstance(node, ast.For) for node in ast.walk(ast.parse(source)))


def _task_number(task_id: str) -> int:
    prefix = "user_task_"
    if not task_id.startswith(prefix):
        raise AuditContractError(f"unexpected task id: {task_id}")
    return int(task_id.removeprefix(prefix))


def _python_literal(value: Any) -> str:
    value = _jsonable(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_python_literal(item) for item in value) + "]"
    if isinstance(value, dict):
        entries = (
            f"{_python_literal(key)}: {_python_literal(item)}"
            for key, item in value.items()
        )
        return "{" + ", ".join(entries) + "}"
    raise AuditContractError(f"cannot render Python literal for {type(value).__name__}")


def _positional_arguments(
    *,
    tool: str,
    args_by_name: dict[str, Any],
    parameter_order: list[str],
    parameter_fields: dict[str, Any],
) -> tuple[list[Any], list[dict[str, str]]]:
    unknown = set(args_by_name) - set(parameter_order)
    if unknown:
        raise AuditContractError(f"unknown arguments for {tool}: {sorted(unknown)}")
    supplied_indices = [parameter_order.index(name) for name in args_by_name]
    last_supplied = max(supplied_indices, default=-1)
    positional: list[Any] = []
    filled_defaults: list[dict[str, str]] = []
    for name in parameter_order[: last_supplied + 1]:
        if name in args_by_name:
            positional.append(args_by_name[name])
            continue
        field = parameter_fields[name]
        if field.is_required():
            raise AuditContractError(f"required argument {tool}.{name} is missing")
        default = field.get_default(call_default_factory=True)
        positional.append(_jsonable(default))
        filled_defaults.append(
            {"parameter": name, "rendered_default": repr(default)}
        )
    return positional, filled_defaults


def _reference_run(
    trace: list[dict[str, Any]],
    tool_params: dict[str, list[str]],
    tool_fields: dict[str, dict[str, Any]],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    lines = ["def run():"]
    positional_trace: list[dict[str, Any]] = []
    filled_defaults: list[dict[str, Any]] = []
    for call_index, call in enumerate(trace):
        tool = call["tool"]
        args_by_name = call["args"]
        positional, defaults = _positional_arguments(
            tool=tool,
            args_by_name=args_by_name,
            parameter_order=tool_params[tool],
            parameter_fields=tool_fields[tool],
        )
        rendered = ", ".join(_python_literal(value) for value in positional)
        lines.append(f"    {tool}({rendered})")
        positional_trace.append({"tool": tool, "args": positional})
        filled_defaults.extend(
            {"tool_call_index": call_index, "tool": tool, **row}
            for row in defaults
        )
    return "\n".join(lines) + "\n", positional_trace, filled_defaults


def _profile_result(
    source: str,
    tool_names: set[str],
    signer: dict[str, str],
    profile: str,
) -> dict[str, Any]:
    try:
        prepare(source, tool_names, signer, dsl_profile=profile)
    except DSLRejectionError as exc:
        return {"accepted": False, "rejection": str(exc)}
    return {"accepted": True, "rejection": None}


def _verify_package_contract() -> None:
    installed = importlib.metadata.version("agentdojo")
    if installed != AGENTDOJO_PACKAGE_VERSION:
        raise AuditContractError(
            "agentdojo package drift: expected "
            f"{AGENTDOJO_PACKAGE_VERSION}, found {installed}"
        )


@functools.lru_cache(maxsize=1)
def build_audit() -> dict[str, Any]:
    """Build and validate the complete 97-task audit."""
    _verify_package_contract()
    native_suites = get_suites(AGENTDOJO_SUITE_VERSION)
    if set(native_suites) != set(SUITE_ORDER):
        raise AuditContractError(
            f"AgentDojo suite drift: observed {sorted(native_suites)}"
        )

    tasks: list[dict[str, Any]] = []
    observed_counts: dict[str, int] = {}
    observed_reference_tool_calls = 0
    for suite_name in SUITE_ORDER:
        suite = native_suites[suite_name]
        task_ids = sorted(suite.user_tasks, key=_task_number)
        observed_counts[suite_name] = len(task_ids)
        expected_ids = [
            f"user_task_{index}"
            for index in range(EXPECTED_SUITE_COUNTS[suite_name])
        ]
        if task_ids != expected_ids:
            raise AuditContractError(
                f"AgentDojo task-ID drift in {suite_name}: observed {task_ids}"
            )
        tool_params = {
            tool.name: list(tool.parameters.model_fields)
            for tool in suite.tools
        }
        tool_fields = {
            tool.name: tool.parameters.model_fields
            for tool in suite.tools
        }
        tool_names = set(tool_params)
        signer = {tool: suite_name for tool in tool_names}
        environment = suite.load_and_inject_default_environment({})

        for task_id in task_ids:
            task = suite.user_tasks[task_id]
            key = f"{suite_name}.{task_id}"
            trace = [
                {
                    "tool": call.function,
                    "args": _jsonable(call.args),
                }
                for call in task.ground_truth(environment)
            ]
            source, positional_trace, filled_defaults = _reference_run(
                trace, tool_params, tool_fields
            )
            observed_reference_tool_calls += len(trace)
            annotation = FANOUT_CANDIDATES.get(key)
            if annotation is None:
                fanout = {
                    "status": "not_candidate",
                    "runtime_cardinality_dependent_individual_calls": False,
                    "fanout_kind": None,
                    "no_bulk_equivalent": None,
                    "partial_bounded_for_prefix": False,
                    "full_task_g2_only_witness": False,
                    "g2_blocker_category": None,
                    "g2_blocker_rationale": None,
                    "non_candidate_reason_class": _non_candidate_reason(
                        suite_name, _task_number(task_id)
                    ),
                    "reference_trace_caveat": None,
                    "borderline_exclusion_rationale": _BORDERLINE_EXCLUSIONS.get(
                        key
                    ),
                }
            else:
                fanout = {
                    "status": "candidate_blocked_in_current_g2",
                    "runtime_cardinality_dependent_individual_calls": True,
                    "fanout_kind": annotation.fanout_kind,
                    "no_bulk_equivalent": True,
                    "partial_bounded_for_prefix": (
                        annotation.partial_bounded_for_prefix
                    ),
                    "full_task_g2_only_witness": False,
                    "g2_blocker_category": annotation.blocker_category,
                    "g2_blocker_rationale": annotation.blocker_rationale,
                    "non_candidate_reason_class": None,
                    "reference_trace_caveat": annotation.reference_trace_caveat,
                    "borderline_exclusion_rationale": None,
                }

            profiles = {
                profile: _profile_result(source, tool_names, signer, profile)
                for profile in ("g1", "g2")
            }
            tasks.append(
                {
                    "task_key": key,
                    "suite": suite_name,
                    "task_id": task_id,
                    "prompt": task.PROMPT,
                    "task_sha256": _json_sha256(
                        {
                            "suite": suite_name,
                            "task_id": task_id,
                            "prompt": task.PROMPT,
                        }
                    ),
                    "reference_trace": trace,
                    "reference_trace_sha256": _json_sha256(trace),
                    "reference_trace_length": len(trace),
                    "reference_positional_trace_sha256": _json_sha256(
                        positional_trace
                    ),
                    "filled_parameter_defaults": filled_defaults,
                    "reference_run_sha256": _source_sha256(source),
                    "reference_run_contains_for": _contains_for(source),
                    "fixed_trace_profile_acceptance": profiles,
                    "fanout_audit": fanout,
                }
            )

    if observed_counts != EXPECTED_SUITE_COUNTS:
        raise AuditContractError(
            f"AgentDojo task-count drift: {observed_counts!r}"
        )
    if observed_reference_tool_calls != EXPECTED_REFERENCE_TOOL_CALLS:
        raise AuditContractError(
            "AgentDojo reference-trace drift: expected "
            f"{EXPECTED_REFERENCE_TOOL_CALLS} tool calls, found "
            f"{observed_reference_tool_calls}"
        )
    keys = [row["task_key"] for row in tasks]
    if len(tasks) != EXPECTED_TASK_COUNT or len(set(keys)) != EXPECTED_TASK_COUNT:
        raise AuditContractError(
            f"expected {EXPECTED_TASK_COUNT} unique tasks, found {len(set(keys))}"
        )
    missing_annotations = set(FANOUT_CANDIDATES) - set(keys)
    if missing_annotations:
        raise AuditContractError(
            f"candidate annotations refer to missing tasks: {sorted(missing_annotations)}"
        )

    candidate_rows = [
        row
        for row in tasks
        if row["fanout_audit"][
            "runtime_cardinality_dependent_individual_calls"
        ]
    ]
    if len(candidate_rows) != EXPECTED_CANDIDATE_COUNT:
        raise AuditContractError(
            "candidate-count drift: expected "
            f"{EXPECTED_CANDIDATE_COUNT}, found {len(candidate_rows)}"
        )

    g1_accepted = sum(
        row["fixed_trace_profile_acceptance"]["g1"]["accepted"]
        for row in tasks
    )
    g2_accepted = sum(
        row["fixed_trace_profile_acceptance"]["g2"]["accepted"]
        for row in tasks
    )
    g2_only = [
        row["task_key"]
        for row in tasks
        if not row["fixed_trace_profile_acceptance"]["g1"]["accepted"]
        and row["fixed_trace_profile_acceptance"]["g2"]["accepted"]
    ]
    expected_g2_only = ["workspace.user_task_33"]
    if (g1_accepted, g2_accepted, g2_only) != (96, 97, expected_g2_only):
        raise AuditContractError(
            "fixed-trace grammar result drift: expected G1=96, G2=97, "
            f"G2-only={expected_g2_only}; observed "
            f"G1={g1_accepted}, G2={g2_accepted}, G2-only={g2_only}"
        )
    full_task_g2_only_witnesses = [
        row["task_key"]
        for row in tasks
        if row["fanout_audit"]["full_task_g2_only_witness"]
    ]
    bounded_for_gain_tasks = [
        row["task_key"]
        for row in tasks
        if not row["fixed_trace_profile_acceptance"]["g1"]["accepted"]
        and row["fixed_trace_profile_acceptance"]["g2"]["accepted"]
        and row["reference_run_contains_for"]
    ]

    corpus_digest_rows = [
        {
            "task_key": row["task_key"],
            "task_sha256": row["task_sha256"],
            "reference_trace_sha256": row["reference_trace_sha256"],
        }
        for row in tasks
    ]
    corpus_sha256 = _json_sha256(corpus_digest_rows)
    if corpus_sha256 != EXPECTED_CORPUS_SHA256:
        raise AuditContractError(
            "AgentDojo prompt/reference-trace hash drift: expected "
            f"{EXPECTED_CORPUS_SHA256}, found {corpus_sha256}"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact": "AgentDojo runtime-cardinality fan-out audit",
        "contract": {
            "agentdojo_package_version": AGENTDOJO_PACKAGE_VERSION,
            "agentdojo_suite_version": AGENTDOJO_SUITE_VERSION,
            "expected_task_count": EXPECTED_TASK_COUNT,
            "expected_reference_tool_calls": EXPECTED_REFERENCE_TOOL_CALLS,
            "expected_suite_counts": EXPECTED_SUITE_COUNTS,
            "suite_order": list(SUITE_ORDER),
            "corpus_sha256": corpus_sha256,
            "task_hash_input": "suite, task_id, prompt",
            "reference_trace_hash_input": (
                "ordered ground_truth() tool names and named arguments"
            ),
        },
        "assumptions": [
            (
                "The unit is one published AgentDojo v1 task in its default "
                "environment."
            ),
            (
                "The audit reads the prompt, ground_truth() sequence, and "
                "installed tool signatures."
            ),
            (
                "A candidate requires individual tool-call multiplicity driven "
                "by a runtime collection and no equivalent bulk tool."
            ),
            (
                "A partial all-elements read prefix does not count as a full-task "
                "G2 witness."
            ),
            (
                "The finite ground_truth() sequence is a reference trace, not a "
                "proof over alternative environment states."
            ),
            (
                "The 11 candidate labels and G2 blockers are reviewed annotations, "
                "not inferred statistics."
            ),
            (
                "The classification is conservative. It excludes ambiguous "
                "content-driven multiplicity unless the prompt explicitly "
                "quantifies over all or each item."
            ),
        ],
        "fanout_semantic_findings": {
            "scope": "runtime-cardinality-dependent individual tool-call fan-out",
            "candidate_tasks": len(candidate_rows),
            "non_candidate_tasks": len(tasks) - len(candidate_rows),
            "partial_bounded_for_prefixes": sum(
                row["fanout_audit"]["partial_bounded_for_prefix"]
                for row in candidate_rows
            ),
            "full_task_g2_only_witnesses": len(full_task_g2_only_witnesses),
            "full_task_g2_only_witness_denominator": EXPECTED_TASK_COUNT,
            "full_task_g2_only_witness_task_keys": full_task_g2_only_witnesses,
            "bounded_for_empirical_gain_tasks": len(bounded_for_gain_tasks),
            "bounded_for_empirical_gain_task_keys": bounded_for_gain_tasks,
            "borderline_excluded_tasks": _BORDERLINE_EXCLUSIONS,
            "evidence_basis": (
                "Full-task witness status is a reviewed annotation. Fixed-trace "
                "bounded-for gain is computed from the generated run syntax and "
                "the two profile checks."
            ),
        },
        "fixed_reference_trace_check": {
            "scope": "literalized default-environment ground_truth() sequences",
            "g1_accepted": g1_accepted,
            "g2_accepted": g2_accepted,
            "g2_only_tasks": g2_only,
            "g2_only_feature_by_task": {
                "workspace.user_task_33": "dict_literal"
            },
            "bounded_for_contribution_tasks": bounded_for_gain_tasks,
            "interpretation": (
                "Every reference sequence is finite and contains no for statement. "
                "The single G2-only acceptance comes from a dict literal used for "
                "an email attachment, not from bounded for."
            ),
        },
        "tasks": tasks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    rendered = json.dumps(build_audit(), ensure_ascii=False, indent=2) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
