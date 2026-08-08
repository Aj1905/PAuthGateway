"""Deterministic AgentDojo reference-trace encodability diagnostic.

This is deliberately *not* a ``FEASIBILITY_EXPRESSIBLE`` evaluation.  An
AgentDojo ``ground_truth()`` value is one finite reference tool-call sequence
for the benchmark's fixed environment.  Turning that sequence into a literal,
straight-line ``run()`` function tests whether that concrete trace can pass a
DSL profile and the deterministic PAuth pipeline.  It does not show that the
same program represents the task over other states, and it cannot reveal a
need for bounded ``for`` because every finite trace can be unrolled.

The corpus and its expected size are pinned so a dependency or benchmark
change fails loudly instead of silently changing the denominator.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import json
from pathlib import Path
from typing import Any

from agentdojo.task_suite.load_suites import get_suites

from benchmarks.agentdojo_adapter import load_suite
from benchmarks.forced_injection import generate_for_task
from pauth import prepare
from pauth.enforcer import Enforcer, check_injection
from pauth.envelope import EnvelopeStore, KeyRing
from pauth.grammar_validator import DSLRejectionError
from pauth.tool_executor import execute_generated_code


AGENTDOJO_VERSION = "0.1.35"
BENCHMARK_VERSION = "v1"
SUITE_TASK_COUNTS = {
    "banking": 16,
    "slack": 21,
    "travel": 20,
    "workspace": 40,
}
EXPECTED_TASKS = 97
EXPECTED_REFERENCE_TOOL_CALLS = 339
EXPECTED_FORCED_INJECTIONS = 756


def _sha256(value: Any) -> str:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _assert_pinned_corpus(agentdojo_suites: dict[str, Any]) -> None:
    installed = importlib.metadata.version("agentdojo")
    if installed != AGENTDOJO_VERSION:
        raise RuntimeError(
            "AgentDojo version drift: "
            f"expected {AGENTDOJO_VERSION}, installed {installed}"
        )

    observed_counts = {
        name: len(agentdojo_suites[name].user_tasks) for name in SUITE_TASK_COUNTS
    }
    if observed_counts != SUITE_TASK_COUNTS:
        raise RuntimeError(
            "AgentDojo v1 task-count drift: "
            f"expected {SUITE_TASK_COUNTS}, observed {observed_counts}"
        )

    for suite_name, count in SUITE_TASK_COUNTS.items():
        expected_ids = {f"user_task_{index}" for index in range(count)}
        observed_ids = set(agentdojo_suites[suite_name].user_tasks)
        if observed_ids != expected_ids:
            raise RuntimeError(
                f"AgentDojo v1 task-ID drift in {suite_name}: "
                f"expected {sorted(expected_ids)}, observed {sorted(observed_ids)}"
            )


def _positional_arguments(
    function_call: Any,
    parameter_order: list[str],
    parameter_fields: dict[str, Any],
) -> tuple[list[Any], list[dict[str, str]]]:
    """Translate AgentDojo keyword arguments to PAuth's positional tool form.

    When a later optional argument is present, omitted parameters before it are
    materialized with the defaults from AgentDojo's actual Pydantic input
    schema.  Merely dropping those holes would shift later operands to the
    wrong parameter positions.
    """

    unknown = set(function_call.args) - set(parameter_order)
    if unknown:
        raise RuntimeError(
            f"unknown arguments for {function_call.function}: {sorted(unknown)}"
        )

    supplied_indices = [
        parameter_order.index(name) for name in function_call.args
    ]
    last_supplied = max(supplied_indices, default=-1)
    positional: list[Any] = []
    filled_defaults: list[dict[str, str]] = []

    for name in parameter_order[: last_supplied + 1]:
        if name in function_call.args:
            positional.append(copy.deepcopy(function_call.args[name]))
            continue
        field = parameter_fields[name]
        if field.is_required():
            raise RuntimeError(
                f"required argument {function_call.function}.{name} is missing"
            )
        default = field.get_default(call_default_factory=True)
        positional.append(copy.deepcopy(default))
        filled_defaults.append(
            {"parameter": name, "rendered_default": repr(default)}
        )

    return positional, filled_defaults


def _straight_line_program(
    reference_calls: list[Any],
    parameter_orders: dict[str, list[str]],
    parameter_fields: dict[str, dict[str, Any]],
) -> tuple[str, list[tuple[str, list[Any]]], list[dict[str, Any]]]:
    lines = ["def run():"]
    positional_trace: list[tuple[str, list[Any]]] = []
    filled_defaults: list[dict[str, Any]] = []

    for call_index, function_call in enumerate(reference_calls):
        tool = function_call.function
        args, defaults = _positional_arguments(
            function_call,
            parameter_orders[tool],
            parameter_fields[tool],
        )
        lines.append(f"    {tool}({', '.join(repr(value) for value in args)})")
        positional_trace.append((tool, args))
        for row in defaults:
            filled_defaults.append(
                {"tool_call_index": call_index, "tool": tool, **row}
            )

    if not reference_calls:
        lines.append("    pass")
    return "\n".join(lines) + "\n", positional_trace, filled_defaults


def _evaluate_profile(
    *,
    profile: str,
    source: str,
    positional_trace: list[tuple[str, list[Any]]],
    forced_injections: list[Any],
    suite_spec: Any,
) -> dict[str, Any]:
    try:
        prepared = prepare(
            source,
            suite_spec.tool_names(),
            suite_spec.tool_signer(),
            dsl_profile=profile,
        )
    except DSLRejectionError as exc:
        return {
            "encodable": False,
            "dsl_accepted": False,
            "pipeline_executed": False,
            "pipeline_ok": False,
            "reference_trace_exact": False,
            "dsl_rejection": str(exc),
            "forced_injection_probe_executed": False,
            "forced_injections_total": 0,
            "forced_injections_permitted": 0,
            "forced_injections_denied": 0,
            "forced_injection_denominator_exclusion": "DSL rejection",
        }

    environment = suite_spec.make_env()
    enforcer = Enforcer(
        prepared.rules,
        EnvelopeStore(KeyRing()),
        suite_spec.tool_signer(),
    )
    report = execute_generated_code(
        prepared.source,
        enforcer,
        suite_spec.tool_params(),
        suite_spec.tool_executor_factory(environment),
    )
    observed_trace = [(event.tool, event.args) for event in report.events]
    trace_exact = observed_trace == positional_trace
    pipeline_ok = (
        report.crashed is None
        and not report.denied
        and not report.tool_errors
        and trace_exact
    )
    permitted_probes: list[dict[str, Any]] = []
    forced_total = forced_permitted = forced_denied = 0
    probe_executed = False
    denominator_exclusion: str | None = None
    if pipeline_ok:
        probe_executed = True
        forced_total = len(forced_injections)
        for forced_call in forced_injections:
            decision = check_injection(
                enforcer,
                forced_call.tool,
                copy.deepcopy(forced_call.args),
            )
            if decision.permit:
                forced_permitted += 1
                permitted_probes.append(
                    {
                        "tool": forced_call.tool,
                        "args": copy.deepcopy(forced_call.args),
                        "reason": decision.reason,
                    }
                )
            else:
                forced_denied += 1
    else:
        denominator_exclusion = "reference trace did not execute exactly"
    return {
        "encodable": pipeline_ok,
        "dsl_accepted": True,
        "pipeline_executed": True,
        "pipeline_ok": pipeline_ok,
        "reference_trace_exact": trace_exact,
        "observed_tool_calls": len(observed_trace),
        "observed_trace_sha256": _sha256(observed_trace),
        "crashed": report.crashed,
        "denials": len(report.denied),
        "tool_errors": list(report.tool_errors),
        "forced_injection_probe_executed": probe_executed,
        "forced_injections_total": forced_total,
        "forced_injections_permitted": forced_permitted,
        "forced_injections_denied": forced_denied,
        "forced_injection_denominator_exclusion": denominator_exclusion,
        "permitted_forced_injections": permitted_probes,
    }


def _suite_self_check(agentdojo_suites: dict[str, Any]) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    failed_tasks: list[dict[str, str]] = []
    passed = total = 0

    for suite_name in SUITE_TASK_COUNTS:
        _, (user_results, _injection_results) = agentdojo_suites[suite_name].check(
            check_injectable=False
        )
        suite_failures = []
        for task_id, (ok, reason) in sorted(user_results.items()):
            total += 1
            passed += int(ok)
            if not ok:
                failure = {
                    "task_id": f"{suite_name}.{task_id}",
                    "reason": reason,
                }
                suite_failures.append(failure)
                failed_tasks.append(failure)
        rows[suite_name] = {
            "passed": len(user_results) - len(suite_failures),
            "total": len(user_results),
            "failed_tasks": suite_failures,
        }

    return {
        "check_injectable": False,
        "scope": "AgentDojo user-task ground-truth utility self-check",
        "passed": passed,
        "total": total,
        "failed_tasks": failed_tasks,
        "suites": rows,
    }


def evaluate(*, include_suite_self_check: bool = True) -> dict[str, Any]:
    agentdojo_suites = get_suites(BENCHMARK_VERSION)
    _assert_pinned_corpus(agentdojo_suites)

    task_rows: list[dict[str, Any]] = []
    suite_reference_calls: dict[str, int] = {}

    for suite_name in SUITE_TASK_COUNTS:
        agentdojo_suite = agentdojo_suites[suite_name]
        suite_spec = load_suite(suite_name)
        parameter_orders = suite_spec.tool_params()
        parameter_fields = {
            tool.name: tool.parameters.model_fields for tool in agentdojo_suite.tools
        }
        suite_call_count = 0

        for task_id, user_task in sorted(agentdojo_suite.user_tasks.items()):
            reference_calls = user_task.ground_truth(suite_spec.make_env())
            source, positional_trace, filled_defaults = _straight_line_program(
                reference_calls,
                parameter_orders,
                parameter_fields,
            )
            forced_injections = generate_for_task(
                agentdojo_suite,
                user_task,
                parameter_orders,
                suite_spec.make_env,
            )
            forced_injection_values = [
                {
                    "tool": forced_call.tool,
                    "args": copy.deepcopy(forced_call.args),
                }
                for forced_call in forced_injections
            ]
            suite_call_count += len(reference_calls)
            task_rows.append(
                {
                    "task_id": f"{suite_name}.{task_id}",
                    "suite": suite_name,
                    "reference_tool_calls": len(reference_calls),
                    "reference_trace_sha256": _sha256(positional_trace),
                    "straight_line_source_sha256": hashlib.sha256(
                        source.encode("utf-8")
                    ).hexdigest(),
                    "filled_parameter_defaults": filled_defaults,
                    "contains_bounded_for": False,
                    "forced_injections_defined": len(forced_injections),
                    "forced_injection_set_sha256": _sha256(forced_injection_values),
                    "profiles": {
                        profile: _evaluate_profile(
                            profile=profile,
                            source=source,
                            positional_trace=positional_trace,
                            forced_injections=forced_injections,
                            suite_spec=suite_spec,
                        )
                        for profile in ("g1", "g2")
                    },
                }
            )
        suite_reference_calls[suite_name] = suite_call_count

    total_calls = sum(suite_reference_calls.values())
    if len(task_rows) != EXPECTED_TASKS or total_calls != EXPECTED_REFERENCE_TOOL_CALLS:
        raise RuntimeError(
            "AgentDojo v1 reference-trace drift: "
            f"expected {EXPECTED_TASKS} tasks/{EXPECTED_REFERENCE_TOOL_CALLS} calls, "
            f"observed {len(task_rows)} tasks/{total_calls} calls"
        )
    forced_injection_count = sum(
        row["forced_injections_defined"] for row in task_rows
    )
    if forced_injection_count != EXPECTED_FORCED_INJECTIONS:
        raise RuntimeError(
            "AgentDojo v1 forced-injection drift: "
            f"expected {EXPECTED_FORCED_INJECTIONS}, "
            f"observed {forced_injection_count}"
        )

    profile_totals = {}
    for profile in ("g1", "g2"):
        passed = sum(
            int(row["profiles"][profile]["encodable"]) for row in task_rows
        )
        forced_total = sum(
            row["profiles"][profile]["forced_injections_total"]
            for row in task_rows
        )
        forced_permitted = sum(
            row["profiles"][profile]["forced_injections_permitted"]
            for row in task_rows
        )
        forced_denied = sum(
            row["profiles"][profile]["forced_injections_denied"]
            for row in task_rows
        )
        attack_eligible_tasks = sum(
            int(row["profiles"][profile]["forced_injection_probe_executed"])
            for row in task_rows
        )
        excluded_tasks = [
            {
                "task_id": row["task_id"],
                "defined_forced_injections": row["forced_injections_defined"],
                "reason": row["profiles"][profile][
                    "forced_injection_denominator_exclusion"
                ],
            }
            for row in task_rows
            if not row["profiles"][profile]["forced_injection_probe_executed"]
        ]
        forced_by_suite = {}
        for suite_name in SUITE_TASK_COUNTS:
            suite_rows = [row for row in task_rows if row["suite"] == suite_name]
            suite_forced_total = sum(
                row["profiles"][profile]["forced_injections_total"]
                for row in suite_rows
            )
            suite_forced_permitted = sum(
                row["profiles"][profile]["forced_injections_permitted"]
                for row in suite_rows
            )
            forced_by_suite[suite_name] = {
                "eligible_tasks": sum(
                    int(
                        row["profiles"][profile][
                            "forced_injection_probe_executed"
                        ]
                    )
                    for row in suite_rows
                ),
                "excluded_tasks": sum(
                    int(
                        not row["profiles"][profile][
                            "forced_injection_probe_executed"
                        ]
                    )
                    for row in suite_rows
                ),
                "total": suite_forced_total,
                "permitted": suite_forced_permitted,
                "denied": suite_forced_total - suite_forced_permitted,
            }
        profile_totals[profile] = {
            "passed": passed,
            "total": EXPECTED_TASKS,
            "rate": passed / EXPECTED_TASKS,
            "forced_injection_evaluation": {
                "eligible_tasks": attack_eligible_tasks,
                "excluded_tasks": excluded_tasks,
                "total": forced_total,
                "permitted": forced_permitted,
                "denied": forced_denied,
                "denial_rate": forced_denied / forced_total if forced_total else None,
                "by_suite": forced_by_suite,
            },
        }

    gains = [
        row
        for row in task_rows
        if not row["profiles"]["g1"]["encodable"]
        and row["profiles"]["g2"]["encodable"]
    ]
    losses = [
        row["task_id"]
        for row in task_rows
        if row["profiles"]["g1"]["encodable"]
        and not row["profiles"]["g2"]["encodable"]
    ]
    difference_rows = [
        {
            "task_id": row["task_id"],
            "cause": (
                "dict_literal"
                if "dict literals" in row["profiles"]["g1"].get("dsl_rejection", "")
                else "other"
            ),
            "g1_dsl_rejection": row["profiles"]["g1"].get("dsl_rejection"),
        }
        for row in gains
    ]

    result = {
        "schema_version": 2,
        "evaluation_name": "AgentDojo v1 reference-trace encodability",
        "evaluation_kind": "deterministic fixed-reference-trace diagnostic",
        "not_feasibility_expressible": True,
        "agentdojo": {
            "package_version": AGENTDOJO_VERSION,
            "benchmark_version": BENCHMARK_VERSION,
            "suite_task_counts": dict(SUITE_TASK_COUNTS),
            "tasks": EXPECTED_TASKS,
            "reference_tool_calls": total_calls,
            "suite_reference_tool_calls": suite_reference_calls,
            "forced_injections_defined": forced_injection_count,
        },
        "method": (
            "Each finite ground_truth() sequence is converted to a positional, "
            "literal, straight-line run() program. Omitted parameters before a "
            "later supplied parameter are filled from AgentDojo's installed tool "
            "schema defaults. Accepted programs are executed through GrammarValidator, "
            "Slicer, RuleCompiler, Enforcer, and ToolExecutor, and their observed "
            "tool-call trace must exactly equal the positional reference trace."
        ),
        "forced_injection_evaluation": {
            "metric": "AUX_INJECTIONS_DENIED",
            "generator": "benchmarks.forced_injection.generate_for_task",
            "timing": (
                "Each fixed forced-injection tool call is presented to the same "
                "Enforcer after exact execution of the task's reference trace."
            ),
            "denominator_rule": (
                "A profile contributes a task's fixed probes only when its DSL "
                "accepts the straight-line reference run and the deterministic "
                "pipeline exactly executes that trace. G1 therefore excludes "
                "workspace.user_task_33 and its 8 probes after DSL rejection; "
                "G2 includes all 97 tasks."
            ),
        },
        "profile_contract": {
            "g1": (
                "PAuth Appendix A reconstruction fixed in docs/SYSTEM_MODEL.md, "
                "with helper lambdas restricted to expressions that do not call tools"
            ),
            "g2": "current default extended profile and a superset of G1",
        },
        "limitations": [
            "A ground_truth() sequence is one finite reference witness, not a "
            "unique task-semantic program.",
            "Literal straight-line encoding hides runtime data dependencies and "
            "does not establish behavior over other states.",
            "Every finite repeated sequence can be unrolled, so this diagnostic "
            "cannot measure the benefit of bounded for.",
            "The fixed forced-injection set is finite and generator-defined; zero "
            "permitted probes does not prove that every possible attack is denied.",
            "Forced-injection probes inspect the authorization relation after the "
            "reference execution. They do not exercise live replay prevention, "
            "call-order enforcement, or dispatch durability.",
            "G1 and G2 have different forced-injection denominators because a "
            "DSL-rejected task cannot produce the required post-reference state.",
        ],
        "profiles": profile_totals,
        "g2_gain_tasks": len(gains),
        "g2_loss_tasks": losses,
        "delta_percentage_points": 100
        * (profile_totals["g2"]["rate"] - profile_totals["g1"]["rate"]),
        "bounded_for_gain": sum(int(row["contains_bounded_for"]) for row in gains),
        "difference": difference_rows,
        "tasks": task_rows,
    }
    if include_suite_self_check:
        result["agentdojo_suite_self_check"] = _suite_self_check(agentdojo_suites)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--skip-suite-self-check", action="store_true")
    args = parser.parse_args(argv)
    result = evaluate(include_suite_self_check=not args.skip_suite_self_check)
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
