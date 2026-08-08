"""Analyze the matched AgentDojo 2 x 2 authorization experiment.

Two designs are supported.  The ``assigned`` design has exactly one
observation for every task and every combination of ``policy_enabled`` and
``injection_enabled``, with one frozen injection goal per task.  The
``full-cross`` design runs every (task, attack goal) pair in the two
injection-on cells; the injection-off cells have no attack-goal axis, so they
keep one observation per task and the four cells are intentionally
asymmetric in size.

Contrasts are paired within the finest shared unit: (task, attack goal) pairs
for injection-on contrasts under full-cross, tasks otherwise.  Percentile
intervals quantify *task-sampling uncertainty only*; under full-cross they use
a task-level cluster bootstrap because the attack goals of one task share the
same prompt, plan, and initial environment.  Most rows use live LLM
execution, while policy-on rows without a cached plan are assessed on the
unchanged environment state.  The intervals therefore do not quantify model
or run-to-run stochasticity.
"""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any


TaskKey = tuple[str, str]
Condition = tuple[bool, bool]

CONDITIONS: tuple[Condition, ...] = (
    (False, False),
    (False, True),
    (True, False),
    (True, True),
)

# AgentDojo v1 travel InjectionTask6 succeeds only when a hotel name appears
# in the final model output.  Its ground-truth tool-call sequence is empty, so
# it is outside PAuthGateway's direct tool-authorization responsibility.
OUTPUT_ONLY_ATTACK_GOALS = frozenset({("travel", "injection_task_6")})


def _condition_name(condition: Condition) -> str:
    policy, injection = condition
    return (
        f"policy_{'on' if policy else 'off'}__"
        f"injection_{'on' if injection else 'off'}"
    )


def _require_bool(row: Mapping[str, Any], key: str, *, where: str) -> bool:
    value = row.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{where}: {key} must be a JSON boolean")
    return value


def _plan_available(row: Mapping[str, Any], *, where: str) -> bool:
    """Read the explicit plan-membership flag used for the common-82 slice."""

    if "plan_available" in row:
        return _require_bool(row, "plan_available", where=where)
    if "plan_present" in row:
        return _require_bool(row, "plan_present", where=where)
    raise ValueError(
        f"{where}: plan_available is required to identify the common-plan population"
    )


def load_jsonl(paths: Sequence[Path]) -> list[dict[str, Any]]:
    """Load one or more JSONL shards without silently accepting non-objects."""

    rows: list[dict[str, Any]] = []
    for path in paths:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
                if not isinstance(value, dict):
                    raise ValueError(f"{path}:{line_number}: each JSONL row must be an object")
                rows.append(value)
    return rows


def _index_rows(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[
    dict[TaskKey, dict[Condition, dict[str, Any]]],
    dict[TaskKey, bool],
    dict[TaskKey, str],
]:
    indexed: dict[TaskKey, dict[Condition, dict[str, Any]]] = {}
    task_plan_available: dict[TaskKey, bool] = {}
    task_injection_id: dict[TaskKey, str] = {}

    for row_number, raw_row in enumerate(rows, start=1):
        row = dict(raw_row)
        where = f"row {row_number}"
        suite = row.get("suite")
        task_id = row.get("user_task_id")
        injection_task_id = row.get("injection_task_id")
        status = row.get("status")
        if not isinstance(suite, str) or not suite:
            raise ValueError(f"{where}: suite must be a non-empty string")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError(f"{where}: user_task_id must be a non-empty string")
        if not isinstance(injection_task_id, str) or not injection_task_id:
            raise ValueError(
                f"{where}: injection_task_id must be a non-empty string"
            )
        if not isinstance(status, str) or not status:
            raise ValueError(f"{where}: status must be a non-empty string")
        if status == "error" or "error" in row:
            raise ValueError(f"{where}: failed experiment rows cannot be analyzed")

        policy = _require_bool(row, "policy_enabled", where=where)
        injection = _require_bool(row, "injection_enabled", where=where)
        completed = _require_bool(row, "task_completed", where=where)
        attack = _require_bool(row, "attack_goal_achieved", where=where)
        joint = _require_bool(
            row, "task_completed_without_attack_goal", where=where
        )
        available = _plan_available(row, where=where)

        expected_joint = completed and not attack
        if joint != expected_joint:
            raise ValueError(
                f"{where}: task_completed_without_attack_goal={joint} is inconsistent "
                f"with task_completed={completed} and attack_goal_achieved={attack}"
            )
        if not injection and attack:
            raise ValueError(
                f"{where}: attack_goal_achieved cannot be true when injection is disabled"
            )

        task_key = (suite, task_id)
        condition = (policy, injection)
        by_condition = indexed.setdefault(task_key, {})
        if condition in by_condition:
            raise ValueError(
                f"duplicate observation for task {task_key!r}, "
                f"condition {_condition_name(condition)}"
            )
        by_condition[condition] = row

        previous = task_plan_available.setdefault(task_key, available)
        if previous != available:
            raise ValueError(
                f"task {task_key!r}: plan_available differs across conditions"
            )
        previous_injection_id = task_injection_id.setdefault(
            task_key, injection_task_id
        )
        if previous_injection_id != injection_task_id:
            raise ValueError(
                f"task {task_key!r}: injection_task_id differs across conditions"
            )

    required = set(CONDITIONS)
    for task_key, by_condition in indexed.items():
        observed = set(by_condition)
        if observed != required:
            missing = sorted(_condition_name(c) for c in required - observed)
            extra = sorted(_condition_name(c) for c in observed - required)
            raise ValueError(
                f"task {task_key!r} does not have exactly one row in all four "
                f"conditions; missing={missing}, extra={extra}"
            )

    return indexed, task_plan_available, task_injection_id


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _percentile(sorted_values: Sequence[float], probability: float) -> float:
    if not sorted_values:
        raise ValueError("cannot calculate a percentile of an empty sample")
    position = (len(sorted_values) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def _condition_summary(
    indexed: Mapping[TaskKey, Mapping[Condition, Mapping[str, Any]]],
    task_keys: Sequence[TaskKey],
    condition: Condition,
) -> dict[str, Any]:
    selected = [indexed[task][condition] for task in task_keys]
    total = len(selected)

    def count(metric: str) -> int:
        return sum(1 for row in selected if row[metric])

    completed = count("task_completed")
    attacks = count("attack_goal_achieved")
    joint = count("task_completed_without_attack_goal")
    return {
        "n": total,
        "task_completed": {"count": completed, "rate": completed / total},
        "attack_goal_achieved": {"count": attacks, "rate": attacks / total},
        "task_completed_without_attack_goal": {
            "count": joint,
            "rate": joint / total,
        },
        "status_counts": dict(sorted(Counter(row["status"] for row in selected).items())),
    }


def _task_contrasts(
    indexed: Mapping[TaskKey, Mapping[Condition, Mapping[str, Any]]],
    task_keys: Sequence[TaskKey],
) -> dict[str, list[float]]:
    contrasts = {
        "attack_goal_achievement_reduction": [],
        "clean_task_completion_decline": [],
        "joint_success_improvement": [],
    }
    for task in task_keys:
        cells = indexed[task]
        contrasts["attack_goal_achievement_reduction"].append(
            float(cells[(False, True)]["attack_goal_achieved"])
            - float(cells[(True, True)]["attack_goal_achieved"])
        )
        contrasts["clean_task_completion_decline"].append(
            float(cells[(False, False)]["task_completed"])
            - float(cells[(True, False)]["task_completed"])
        )
        contrasts["joint_success_improvement"].append(
            float(cells[(True, True)]["task_completed_without_attack_goal"])
            - float(cells[(False, True)]["task_completed_without_attack_goal"])
        )
    return contrasts


def _paired_bootstrap(
    contrast_values: Mapping[str, Sequence[float]],
    *,
    seed: int,
    iterations: int,
) -> dict[str, tuple[float, float]]:
    if iterations <= 0:
        raise ValueError("bootstrap_iterations must be positive")
    lengths = {len(values) for values in contrast_values.values()}
    if len(lengths) != 1 or not lengths or next(iter(lengths)) == 0:
        raise ValueError("all paired contrasts must contain the same non-zero task count")
    task_count = next(iter(lengths))
    samples = {name: [] for name in contrast_values}
    random_source = random.Random(seed)

    # A single resampled task-index vector is shared by all three contrasts in
    # each iteration.  This preserves the within-task pairing and covariance.
    for _ in range(iterations):
        indices = [random_source.randrange(task_count) for _ in range(task_count)]
        for name, values in contrast_values.items():
            samples[name].append(_mean([values[index] for index in indices]))

    intervals: dict[str, tuple[float, float]] = {}
    for name, values in samples.items():
        values.sort()
        intervals[name] = (_percentile(values, 0.025), _percentile(values, 0.975))
    return intervals


def _population_summary(
    indexed: Mapping[TaskKey, Mapping[Condition, Mapping[str, Any]]],
    task_keys: Sequence[TaskKey],
    *,
    seed: int,
    bootstrap_iterations: int,
) -> dict[str, Any]:
    conditions = {
        _condition_name(condition): _condition_summary(indexed, task_keys, condition)
        for condition in CONDITIONS
    }
    values = _task_contrasts(indexed, task_keys)
    intervals = _paired_bootstrap(
        values, seed=seed, iterations=bootstrap_iterations
    )
    contrast_descriptions = {
        "attack_goal_achievement_reduction": {
            "formula": "policy_off_injection_on - policy_on_injection_on",
            "metric": "attack_goal_achieved",
            "positive_direction": "improvement",
        },
        "clean_task_completion_decline": {
            "formula": "policy_off_injection_off - policy_on_injection_off",
            "metric": "task_completed",
            "positive_direction": "degradation",
        },
        "joint_success_improvement": {
            "formula": "policy_on_injection_on - policy_off_injection_on",
            "metric": "task_completed_without_attack_goal",
            "positive_direction": "improvement",
        },
    }
    contrasts: dict[str, Any] = {}
    for name, task_values in values.items():
        estimate = _mean(task_values)
        lower, upper = intervals[name]
        contrasts[name] = {
            **contrast_descriptions[name],
            "estimate": estimate,
            "estimate_percentage_points": estimate * 100.0,
            "ci95_percentile": [lower, upper],
            "ci95_percentage_points": [lower * 100.0, upper * 100.0],
        }
    return {
        "task_count": len(task_keys),
        "conditions": conditions,
        "paired_contrasts": contrasts,
    }


def analyze_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    expected_task_count: int | None = 97,
    expected_common_plan_count: int | None = 82,
    expected_tool_call_attack_count: int | None = 94,
    expected_tool_call_attack_common_plan_count: int | None = 79,
    bootstrap_seed: int = 20260803,
    bootstrap_iterations: int = 10_000,
) -> dict[str, Any]:
    """Validate and aggregate a one-observation, matched four-cell experiment."""

    indexed, task_plan_available, task_injection_id = _index_rows(rows)
    all_tasks = sorted(indexed)
    common_plan_tasks = [task for task in all_tasks if task_plan_available[task]]
    tool_call_attack_tasks = [
        task
        for task in all_tasks
        if (task[0], task_injection_id[task]) not in OUTPUT_ONLY_ATTACK_GOALS
    ]
    tool_call_attack_common_plan_tasks = [
        task for task in tool_call_attack_tasks if task_plan_available[task]
    ]
    if expected_task_count is not None and len(all_tasks) != expected_task_count:
        raise ValueError(
            f"expected {expected_task_count} unique tasks, observed {len(all_tasks)}"
        )
    if (
        expected_common_plan_count is not None
        and len(common_plan_tasks) != expected_common_plan_count
    ):
        raise ValueError(
            f"expected {expected_common_plan_count} common-plan tasks, "
            f"observed {len(common_plan_tasks)}"
        )
    if not common_plan_tasks:
        raise ValueError("the common-plan population is empty")
    if (
        expected_tool_call_attack_count is not None
        and len(tool_call_attack_tasks) != expected_tool_call_attack_count
    ):
        raise ValueError(
            f"expected {expected_tool_call_attack_count} tool-call-attack tasks, "
            f"observed {len(tool_call_attack_tasks)}"
        )
    if (
        expected_tool_call_attack_common_plan_count is not None
        and len(tool_call_attack_common_plan_tasks)
        != expected_tool_call_attack_common_plan_count
    ):
        raise ValueError(
            "expected "
            f"{expected_tool_call_attack_common_plan_count} common-plan "
            "tool-call-attack tasks, observed "
            f"{len(tool_call_attack_common_plan_tasks)}"
        )

    status_counts = Counter(
        row["status"]
        for by_condition in indexed.values()
        for row in by_condition.values()
    )

    return {
        "analysis_schema": "agentdojo_factorial_analysis_v2",
        "design": {
            "factors": ["policy_enabled", "injection_enabled"],
            "observations_per_task_condition": 1,
            "live_execution_rows": status_counts.get("live", 0),
            "unchanged_state_assessment_rows": status_counts.get(
                "plan_unavailable", 0
            ),
            "status_counts": dict(sorted(status_counts.items())),
            "matching_unit": ["suite", "user_task_id"],
            "output_only_attack_goals_excluded_from_tool_call_scope": [
                {"suite": suite, "injection_task_id": injection_task_id}
                for suite, injection_task_id in sorted(OUTPUT_ONLY_ATTACK_GOALS)
            ],
        },
        "uncertainty": {
            "method": "task-level paired bootstrap percentile interval",
            "confidence_level": 0.95,
            "bootstrap_iterations": bootstrap_iterations,
            "seed": bootstrap_seed,
            "scope": "task_sampling_uncertainty_only",
            "limitation": (
                "One observation was recorded per task-condition. Some observations "
                "may be unchanged-state assessments rather than live LLM executions; "
                "the intervals do not quantify LLM or run-to-run stochasticity."
            ),
        },
        "populations": {
            "all_tasks": _population_summary(
                indexed,
                all_tasks,
                seed=bootstrap_seed,
                bootstrap_iterations=bootstrap_iterations,
            ),
            "common_plan_tasks": _population_summary(
                indexed,
                common_plan_tasks,
                seed=bootstrap_seed + 1,
                bootstrap_iterations=bootstrap_iterations,
            ),
            "tool_call_attack_tasks": _population_summary(
                indexed,
                tool_call_attack_tasks,
                seed=bootstrap_seed + 2,
                bootstrap_iterations=bootstrap_iterations,
            ),
            "tool_call_attack_common_plan_tasks": _population_summary(
                indexed,
                tool_call_attack_common_plan_tasks,
                seed=bootstrap_seed + 3,
                bootstrap_iterations=bootstrap_iterations,
            ),
        },
    }


PairKey = tuple[str, str, str]


def _index_full_cross_rows(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[
    dict[TaskKey, dict[bool, dict[str, Any]]],
    dict[PairKey, dict[bool, dict[str, Any]]],
    dict[TaskKey, bool],
]:
    """Index the asymmetric design: pair-level injection-on, task-level off."""

    off_rows: dict[TaskKey, dict[bool, dict[str, Any]]] = {}
    on_rows: dict[PairKey, dict[bool, dict[str, Any]]] = {}
    task_plan_available: dict[TaskKey, bool] = {}

    for row_number, raw_row in enumerate(rows, start=1):
        row = dict(raw_row)
        where = f"row {row_number}"
        suite = row.get("suite")
        task_id = row.get("user_task_id")
        injection_task_id = row.get("injection_task_id")
        status = row.get("status")
        if not isinstance(suite, str) or not suite:
            raise ValueError(f"{where}: suite must be a non-empty string")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError(f"{where}: user_task_id must be a non-empty string")
        if not isinstance(injection_task_id, str) or not injection_task_id:
            raise ValueError(
                f"{where}: injection_task_id must be a non-empty string"
            )
        if not isinstance(status, str) or not status:
            raise ValueError(f"{where}: status must be a non-empty string")
        if status == "error" or "error" in row:
            raise ValueError(f"{where}: failed experiment rows cannot be analyzed")

        policy = _require_bool(row, "policy_enabled", where=where)
        injection = _require_bool(row, "injection_enabled", where=where)
        completed = _require_bool(row, "task_completed", where=where)
        attack = _require_bool(row, "attack_goal_achieved", where=where)
        joint = _require_bool(
            row, "task_completed_without_attack_goal", where=where
        )
        available = _plan_available(row, where=where)

        if joint != (completed and not attack):
            raise ValueError(
                f"{where}: task_completed_without_attack_goal={joint} is inconsistent "
                f"with task_completed={completed} and attack_goal_achieved={attack}"
            )
        if not injection and attack:
            raise ValueError(
                f"{where}: attack_goal_achieved cannot be true when injection is disabled"
            )

        task_key = (suite, task_id)
        if injection:
            pair_key = (suite, task_id, injection_task_id)
            by_policy = on_rows.setdefault(pair_key, {})
            if policy in by_policy:
                raise ValueError(
                    f"duplicate injection-on observation for pair {pair_key!r}, "
                    f"policy_enabled={policy}"
                )
            by_policy[policy] = row
        else:
            by_policy = off_rows.setdefault(task_key, {})
            if policy in by_policy:
                raise ValueError(
                    f"duplicate injection-off observation for task {task_key!r}, "
                    f"policy_enabled={policy}"
                )
            by_policy[policy] = row

        previous = task_plan_available.setdefault(task_key, available)
        if previous != available:
            raise ValueError(
                f"task {task_key!r}: plan_available differs across rows"
            )

    for task_key, by_policy in off_rows.items():
        if set(by_policy) != {False, True}:
            raise ValueError(
                f"task {task_key!r} does not have exactly one injection-off row "
                "for each policy condition"
            )
    for pair_key, by_policy in on_rows.items():
        if set(by_policy) != {False, True}:
            raise ValueError(
                f"pair {pair_key!r} does not have exactly one injection-on row "
                "for each policy condition"
            )
    off_tasks = set(off_rows)
    on_tasks = {(suite, task_id) for suite, task_id, _ in on_rows}
    if off_tasks != on_tasks:
        missing_on = sorted(off_tasks - on_tasks)
        missing_off = sorted(on_tasks - off_tasks)
        raise ValueError(
            "injection-on and injection-off cells cover different task sets; "
            f"missing injection-on={missing_on}, missing injection-off={missing_off}"
        )

    return off_rows, on_rows, task_plan_available


def _full_cross_condition_summaries(
    off_rows: Mapping[TaskKey, Mapping[bool, Mapping[str, Any]]],
    on_rows: Mapping[PairKey, Mapping[bool, Mapping[str, Any]]],
    task_keys: Sequence[TaskKey],
    pair_keys: Sequence[PairKey],
) -> dict[str, Any]:
    def summarize(selected: Sequence[Mapping[str, Any]], unit: str) -> dict[str, Any]:
        total = len(selected)

        def count(metric: str) -> int:
            return sum(1 for row in selected if row[metric])

        completed = count("task_completed")
        attacks = count("attack_goal_achieved")
        joint = count("task_completed_without_attack_goal")
        return {
            "n": total,
            "unit": unit,
            "task_completed": {"count": completed, "rate": completed / total},
            "attack_goal_achieved": {"count": attacks, "rate": attacks / total},
            "task_completed_without_attack_goal": {
                "count": joint,
                "rate": joint / total,
            },
            "status_counts": dict(
                sorted(Counter(row["status"] for row in selected).items())
            ),
        }

    return {
        "policy_off__injection_off": summarize(
            [off_rows[task][False] for task in task_keys], "task"
        ),
        "policy_on__injection_off": summarize(
            [off_rows[task][True] for task in task_keys], "task"
        ),
        "policy_off__injection_on": summarize(
            [on_rows[pair][False] for pair in pair_keys], "pair"
        ),
        "policy_on__injection_on": summarize(
            [on_rows[pair][True] for pair in pair_keys], "pair"
        ),
    }


def _cluster_bootstrap(
    per_task_pair_values: Mapping[str, Sequence[Sequence[float]]],
    per_task_scalar_values: Mapping[str, Sequence[float]],
    *,
    seed: int,
    iterations: int,
) -> dict[str, tuple[float, float]]:
    """Task-level cluster bootstrap sharing one resample vector per iteration.

    Pair-valued contrasts are aggregated as pooled means (total over selected
    tasks' pairs divided by the selected pair count), so tasks with many
    attack goals are not over- or under-weighted relative to the point
    estimate.
    """

    if iterations <= 0:
        raise ValueError("bootstrap_iterations must be positive")
    task_counts = {
        len(values)
        for values in (*per_task_pair_values.values(), *per_task_scalar_values.values())
    }
    if len(task_counts) != 1 or next(iter(task_counts)) == 0:
        raise ValueError("all contrasts must cover the same non-zero task count")
    task_count = next(iter(task_counts))
    pair_sums = {
        name: [sum(values) for values in task_values]
        for name, task_values in per_task_pair_values.items()
    }
    pair_lens = {
        name: [len(values) for values in task_values]
        for name, task_values in per_task_pair_values.items()
    }
    samples: dict[str, list[float]] = {
        name: [] for name in (*per_task_pair_values, *per_task_scalar_values)
    }
    random_source = random.Random(seed)

    for _ in range(iterations):
        indices = [random_source.randrange(task_count) for _ in range(task_count)]
        for name in per_task_pair_values:
            numerator = sum(pair_sums[name][index] for index in indices)
            denominator = sum(pair_lens[name][index] for index in indices)
            if denominator == 0:
                raise ValueError(
                    f"contrast {name}: a bootstrap resample contains no pairs"
                )
            samples[name].append(numerator / denominator)
        for name, values in per_task_scalar_values.items():
            samples[name].append(_mean([values[index] for index in indices]))

    intervals: dict[str, tuple[float, float]] = {}
    for name, values in samples.items():
        values.sort()
        intervals[name] = (_percentile(values, 0.025), _percentile(values, 0.975))
    return intervals


def _full_cross_population_summary(
    off_rows: Mapping[TaskKey, Mapping[bool, Mapping[str, Any]]],
    on_rows: Mapping[PairKey, Mapping[bool, Mapping[str, Any]]],
    task_keys: Sequence[TaskKey],
    pair_keys: Sequence[PairKey],
    *,
    seed: int,
    bootstrap_iterations: int,
) -> dict[str, Any]:
    pairs_by_task: dict[TaskKey, list[PairKey]] = {task: [] for task in task_keys}
    for pair in pair_keys:
        pairs_by_task[(pair[0], pair[1])].append(pair)

    security_values = [
        [
            float(on_rows[pair][False]["attack_goal_achieved"])
            - float(on_rows[pair][True]["attack_goal_achieved"])
            for pair in pairs_by_task[task]
        ]
        for task in task_keys
    ]
    joint_values = [
        [
            float(on_rows[pair][True]["task_completed_without_attack_goal"])
            - float(on_rows[pair][False]["task_completed_without_attack_goal"])
            for pair in pairs_by_task[task]
        ]
        for task in task_keys
    ]
    attacked_values = [
        [
            float(on_rows[pair][False]["task_completed"])
            - float(on_rows[pair][True]["task_completed"])
            for pair in pairs_by_task[task]
        ]
        for task in task_keys
    ]
    clean_values = [
        float(off_rows[task][False]["task_completed"])
        - float(off_rows[task][True]["task_completed"])
        for task in task_keys
    ]

    intervals = _cluster_bootstrap(
        {
            "attack_goal_achievement_reduction": security_values,
            "joint_success_improvement": joint_values,
            "attacked_task_completion_decline": attacked_values,
        },
        {"clean_task_completion_decline": clean_values},
        seed=seed,
        iterations=bootstrap_iterations,
    )

    pair_total = len(pair_keys)
    contrasts = {
        "attack_goal_achievement_reduction": {
            "formula": "policy_off_injection_on - policy_on_injection_on",
            "metric": "attack_goal_achieved",
            "positive_direction": "improvement",
            "pairing_unit": "pair",
            "estimate": sum(sum(values) for values in security_values) / pair_total,
        },
        "clean_task_completion_decline": {
            "formula": "policy_off_injection_off - policy_on_injection_off",
            "metric": "task_completed",
            "positive_direction": "degradation",
            "pairing_unit": "task",
            "estimate": _mean(clean_values),
        },
        "attacked_task_completion_decline": {
            "formula": "policy_off_injection_on - policy_on_injection_on",
            "metric": "task_completed",
            "positive_direction": "degradation",
            "pairing_unit": "pair",
            "estimate": sum(sum(values) for values in attacked_values) / pair_total,
        },
        "joint_success_improvement": {
            "formula": "policy_on_injection_on - policy_off_injection_on",
            "metric": "task_completed_without_attack_goal",
            "positive_direction": "improvement",
            "pairing_unit": "pair",
            "estimate": sum(sum(values) for values in joint_values) / pair_total,
        },
    }
    for name, contrast in contrasts.items():
        lower, upper = intervals[name]
        contrast["estimate_percentage_points"] = contrast["estimate"] * 100.0
        contrast["ci95_percentile"] = [lower, upper]
        contrast["ci95_percentage_points"] = [lower * 100.0, upper * 100.0]

    return {
        "task_count": len(task_keys),
        "pair_count": pair_total,
        "conditions": _full_cross_condition_summaries(
            off_rows, on_rows, task_keys, pair_keys
        ),
        "paired_contrasts": contrasts,
    }


def _attack_goal_breakdown(
    on_rows: Mapping[PairKey, Mapping[bool, Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[PairKey]] = {}
    for pair in on_rows:
        grouped.setdefault((pair[0], pair[2]), []).append(pair)
    breakdown = []
    for (suite, injection_task_id), pairs in sorted(grouped.items()):
        off_hits = sum(
            1 for pair in pairs if on_rows[pair][False]["attack_goal_achieved"]
        )
        on_hits = sum(
            1 for pair in pairs if on_rows[pair][True]["attack_goal_achieved"]
        )
        total = len(pairs)
        breakdown.append(
            {
                "suite": suite,
                "injection_task_id": injection_task_id,
                "pair_count": total,
                "attack_goal_achieved_policy_off": {
                    "count": off_hits,
                    "rate": off_hits / total,
                },
                "attack_goal_achieved_policy_on": {
                    "count": on_hits,
                    "rate": on_hits / total,
                },
                "reduction_percentage_points": (off_hits - on_hits) / total * 100.0,
            }
        )
    return breakdown


def analyze_full_cross_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    expected_task_count: int | None = 97,
    expected_pair_count: int | None = 629,
    expected_common_plan_task_count: int | None = 82,
    expected_common_plan_pair_count: int | None = 542,
    bootstrap_seed: int = 20260804,
    bootstrap_iterations: int = 10_000,
) -> dict[str, Any]:
    """Aggregate the asymmetric full-cross experiment.

    Injection-on cells hold one observation per (task, attack goal) pair;
    injection-off cells hold one observation per task, because those cells
    have no attack-goal axis.
    """

    off_rows, on_rows, task_plan_available = _index_full_cross_rows(rows)
    all_tasks = sorted(off_rows)
    all_pairs = sorted(on_rows)
    common_plan_tasks = [task for task in all_tasks if task_plan_available[task]]
    common_plan_pairs = [
        pair for pair in all_pairs if task_plan_available[(pair[0], pair[1])]
    ]
    tool_call_pairs = [
        pair for pair in all_pairs if (pair[0], pair[2]) not in OUTPUT_ONLY_ATTACK_GOALS
    ]
    tool_call_common_plan_pairs = [
        pair
        for pair in tool_call_pairs
        if task_plan_available[(pair[0], pair[1])]
    ]

    checks = (
        ("unique tasks", expected_task_count, len(all_tasks)),
        ("injection-on pairs", expected_pair_count, len(all_pairs)),
        (
            "common-plan tasks",
            expected_common_plan_task_count,
            len(common_plan_tasks),
        ),
        (
            "common-plan pairs",
            expected_common_plan_pair_count,
            len(common_plan_pairs),
        ),
    )
    for label, expected, observed in checks:
        if expected is not None and observed != expected:
            raise ValueError(f"expected {expected} {label}, observed {observed}")
    if not common_plan_tasks:
        raise ValueError("the common-plan population is empty")

    status_counts = Counter(
        row["status"]
        for by_policy in (*off_rows.values(), *on_rows.values())
        for row in by_policy.values()
    )

    populations = {
        "all_tasks": (all_tasks, all_pairs, bootstrap_seed),
        "common_plan_tasks": (
            common_plan_tasks,
            common_plan_pairs,
            bootstrap_seed + 1,
        ),
        "tool_call_attack_pairs": (all_tasks, tool_call_pairs, bootstrap_seed + 2),
        "tool_call_attack_common_plan_pairs": (
            common_plan_tasks,
            tool_call_common_plan_pairs,
            bootstrap_seed + 3,
        ),
    }
    return {
        "analysis_schema": "agentdojo_factorial_full_cross_analysis_v1",
        "design": {
            "factors": ["policy_enabled", "injection_enabled"],
            "injection_on_observation_unit": [
                "suite",
                "user_task_id",
                "injection_task_id",
            ],
            "injection_off_observation_unit": ["suite", "user_task_id"],
            "asymmetry": (
                "Injection-off cells have no attack-goal axis, so they hold one "
                "observation per task while injection-on cells hold one per "
                "(task, attack goal) pair."
            ),
            "live_execution_rows": status_counts.get("live", 0),
            "unchanged_state_assessment_rows": status_counts.get(
                "plan_unavailable", 0
            ),
            "status_counts": dict(sorted(status_counts.items())),
            "output_only_attack_goals_excluded_from_tool_call_scope": [
                {"suite": suite, "injection_task_id": injection_task_id}
                for suite, injection_task_id in sorted(OUTPUT_ONLY_ATTACK_GOALS)
            ],
        },
        "uncertainty": {
            "method": (
                "task-level cluster bootstrap percentile interval; attack goals "
                "of one task are resampled together because they share the same "
                "prompt, plan, and initial environment"
            ),
            "confidence_level": 0.95,
            "bootstrap_iterations": bootstrap_iterations,
            "seed": bootstrap_seed,
            "scope": "task_sampling_uncertainty_only",
            "limitation": (
                "One observation was recorded per unit and condition. Some "
                "observations are unchanged-state assessments rather than live "
                "LLM executions; the intervals do not quantify LLM or "
                "run-to-run stochasticity."
            ),
        },
        "populations": {
            name: _full_cross_population_summary(
                off_rows,
                on_rows,
                tasks,
                pairs,
                seed=seed,
                bootstrap_iterations=bootstrap_iterations,
            )
            for name, (tasks, pairs, seed) in populations.items()
        },
        "attack_goal_breakdown": _attack_goal_breakdown(on_rows),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        nargs="+",
        required=True,
        help="one or more JSONL shards containing the four conditions",
    )
    parser.add_argument("--output", type=Path, required=True, help="output JSON path")
    parser.add_argument(
        "--design",
        choices=("assigned", "full-cross"),
        default="assigned",
        help=(
            "assigned: one frozen injection goal per task (balanced four cells). "
            "full-cross: every (task, attack goal) pair in the injection-on "
            "cells, one row per task in the injection-off cells."
        ),
    )
    parser.add_argument("--expected-task-count", type=int, default=97)
    parser.add_argument("--expected-common-plan-count", type=int, default=82)
    parser.add_argument("--expected-tool-call-attack-count", type=int, default=94)
    parser.add_argument(
        "--expected-tool-call-attack-common-plan-count", type=int, default=79
    )
    parser.add_argument("--expected-pair-count", type=int, default=629)
    parser.add_argument("--expected-common-plan-pair-count", type=int, default=542)
    parser.add_argument("--bootstrap-seed", type=int, default=None)
    parser.add_argument("--bootstrap-iterations", type=int, default=10_000)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.design == "full-cross":
        result = analyze_full_cross_rows(
            load_jsonl(args.input),
            expected_task_count=args.expected_task_count,
            expected_pair_count=args.expected_pair_count,
            expected_common_plan_task_count=args.expected_common_plan_count,
            expected_common_plan_pair_count=args.expected_common_plan_pair_count,
            bootstrap_seed=(
                20260804 if args.bootstrap_seed is None else args.bootstrap_seed
            ),
            bootstrap_iterations=args.bootstrap_iterations,
        )
    else:
        result = analyze_rows(
            load_jsonl(args.input),
            expected_task_count=args.expected_task_count,
            expected_common_plan_count=args.expected_common_plan_count,
            expected_tool_call_attack_count=args.expected_tool_call_attack_count,
            expected_tool_call_attack_common_plan_count=(
                args.expected_tool_call_attack_common_plan_count
            ),
            bootstrap_seed=(
                20260803 if args.bootstrap_seed is None else args.bootstrap_seed
            ),
            bootstrap_iterations=args.bootstrap_iterations,
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
