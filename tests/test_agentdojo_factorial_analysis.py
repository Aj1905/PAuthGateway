import json

import pytest

from eval.agentdojo_factorial_analysis import (
    analyze_full_cross_rows,
    analyze_rows,
    main,
)


CONDITIONS = ((False, False), (False, True), (True, False), (True, True))


def _fixture_rows():
    # Metric vectors by condition, ordered by tasks a-d.
    completed = {
        (False, False): [True, True, True, True],
        (False, True): [True, True, False, True],
        (True, False): [True, True, False, False],
        (True, True): [True, False, True, False],
    }
    attacks = {
        (False, False): [False, False, False, False],
        (False, True): [True, True, False, True],
        (True, False): [False, False, False, False],
        (True, True): [False, True, False, False],
    }
    rows = []
    for task_index, task_id in enumerate(("a", "b", "c", "d")):
        for policy, injection in CONDITIONS:
            task_completed = completed[(policy, injection)][task_index]
            attack_goal_achieved = attacks[(policy, injection)][task_index]
            rows.append(
                {
                    "suite": "fixture",
                    "user_task_id": task_id,
                    "injection_task_id": "injection_task_0",
                    "policy_enabled": policy,
                    "injection_enabled": injection,
                    "plan_available": task_id != "d",
                    "task_completed": task_completed,
                    "attack_goal_achieved": attack_goal_achieved,
                    "task_completed_without_attack_goal": (
                        task_completed and not attack_goal_achieved
                    ),
                    "status": (
                        "plan_unavailable"
                        if task_id == "d" and policy
                        else "live"
                    ),
                }
            )
    return rows


def _analyze(rows=None, *, seed=7):
    return analyze_rows(
        _fixture_rows() if rows is None else rows,
        expected_task_count=4,
        expected_common_plan_count=3,
        expected_tool_call_attack_count=4,
        expected_tool_call_attack_common_plan_count=3,
        bootstrap_seed=seed,
        bootstrap_iterations=500,
    )


def test_aggregates_all_tasks_and_common_plan_tasks():
    result = _analyze()
    all_tasks = result["populations"]["all_tasks"]
    common = result["populations"]["common_plan_tasks"]

    assert all_tasks["task_count"] == 4
    assert common["task_count"] == 3
    assert all_tasks["conditions"]["policy_off__injection_off"][
        "task_completed"
    ] == {"count": 4, "rate": 1.0}
    assert all_tasks["conditions"]["policy_on__injection_on"][
        "attack_goal_achieved"
    ] == {"count": 1, "rate": 0.25}

    all_contrasts = all_tasks["paired_contrasts"]
    assert all_contrasts["attack_goal_achievement_reduction"]["estimate"] == 0.5
    assert all_contrasts["clean_task_completion_decline"]["estimate"] == 0.5
    assert all_contrasts["joint_success_improvement"]["estimate"] == 0.5

    common_contrasts = common["paired_contrasts"]
    assert common_contrasts["attack_goal_achievement_reduction"][
        "estimate"
    ] == pytest.approx(1 / 3)
    assert common_contrasts["clean_task_completion_decline"][
        "estimate"
    ] == pytest.approx(1 / 3)
    assert common_contrasts["joint_success_improvement"]["estimate"] == pytest.approx(
        2 / 3
    )


def test_reports_tool_call_attack_scope_separately():
    rows = _fixture_rows()
    for row in rows:
        if row["user_task_id"] == "a":
            row["suite"] = "travel"
            row["injection_task_id"] = "injection_task_6"

    result = analyze_rows(
        rows,
        expected_task_count=4,
        expected_common_plan_count=3,
        expected_tool_call_attack_count=3,
        expected_tool_call_attack_common_plan_count=2,
        bootstrap_seed=7,
        bootstrap_iterations=100,
    )

    assert result["analysis_schema"] == "agentdojo_factorial_analysis_v2"
    assert result["populations"]["tool_call_attack_tasks"]["task_count"] == 3
    assert result["populations"]["tool_call_attack_common_plan_tasks"][
        "task_count"
    ] == 2
    assert result["design"][
        "output_only_attack_goals_excluded_from_tool_call_scope"
    ] == [{"suite": "travel", "injection_task_id": "injection_task_6"}]


def test_bootstrap_is_fixed_seed_and_scope_is_explicit():
    first = _analyze(seed=19)
    second = _analyze(seed=19)
    assert first == second
    assert first["uncertainty"]["scope"] == "task_sampling_uncertainty_only"
    assert first["design"]["observations_per_task_condition"] == 1
    assert first["design"]["live_execution_rows"] == 14
    assert first["design"]["unchanged_state_assessment_rows"] == 2
    assert "do not quantify LLM or run-to-run stochasticity" in first[
        "uncertainty"
    ]["limitation"]


def test_rejects_duplicate_task_condition():
    rows = _fixture_rows()
    rows.append(dict(rows[0]))
    with pytest.raises(ValueError, match="duplicate observation"):
        _analyze(rows)


def test_rejects_missing_task_condition():
    rows = _fixture_rows()[1:]
    with pytest.raises(ValueError, match="does not have exactly one row"):
        _analyze(rows)


def test_rejects_inconsistent_joint_metric():
    rows = _fixture_rows()
    rows[0]["task_completed_without_attack_goal"] = False
    with pytest.raises(ValueError, match="is inconsistent"):
        _analyze(rows)


def test_rejects_plan_membership_that_changes_between_conditions():
    rows = _fixture_rows()
    rows[0]["plan_available"] = False
    with pytest.raises(ValueError, match="plan_available differs"):
        _analyze(rows)


def test_rejects_failed_experiment_rows():
    rows = _fixture_rows()
    rows[0]["status"] = "error"
    rows[0]["error"] = "APIConnectionError"
    with pytest.raises(ValueError, match="failed experiment rows"):
        _analyze(rows)


def test_cli_reads_jsonl_shards_and_writes_json(tmp_path):
    rows = _fixture_rows()
    inputs = [tmp_path / "first.jsonl", tmp_path / "second.jsonl"]
    midpoint = len(rows) // 2
    for path, shard in zip(inputs, (rows[:midpoint], rows[midpoint:]), strict=True):
        path.write_text(
            "".join(json.dumps(row) + "\n" for row in shard), encoding="utf-8"
        )
    output = tmp_path / "analysis.json"

    exit_code = main(
        [
            "--input",
            str(inputs[0]),
            str(inputs[1]),
            "--output",
            str(output),
            "--expected-task-count",
            "4",
            "--expected-common-plan-count",
            "3",
            "--expected-tool-call-attack-count",
            "4",
            "--expected-tool-call-attack-common-plan-count",
            "3",
            "--bootstrap-iterations",
            "100",
        ]
    )

    assert exit_code == 0
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["populations"]["all_tasks"]["task_count"] == 4


# --- full-cross design ---


def _full_cross_row(
    suite,
    task_id,
    injection_task_id,
    policy,
    injection,
    *,
    completed,
    attack,
    plan_available=True,
    status="live",
):
    return {
        "suite": suite,
        "user_task_id": task_id,
        "injection_task_id": injection_task_id,
        "policy_enabled": policy,
        "injection_enabled": injection,
        "plan_available": plan_available,
        "task_completed": completed,
        "attack_goal_achieved": attack,
        "task_completed_without_attack_goal": completed and not attack,
        "status": status,
    }


def _full_cross_fixture_rows():
    # Tasks with unequal attack-goal counts: a has 2 goals, b has 3, and the
    # plan-unavailable travel task c has 2 goals including the output-only
    # injection_task_6.
    on_attacks = {
        # (suite, task, goal): (policy_off attack, policy_on attack)
        ("fixture", "a", "goal_0"): (True, False),
        ("fixture", "a", "goal_1"): (True, False),
        ("fixture", "b", "goal_0"): (True, True),
        ("fixture", "b", "goal_1"): (False, False),
        ("fixture", "b", "goal_2"): (True, False),
        ("travel", "c", "injection_task_0"): (True, False),
        ("travel", "c", "injection_task_6"): (True, False),
    }
    off_completed = {
        # (suite, task): (policy_off completed, policy_on completed)
        ("fixture", "a"): (True, True),
        ("fixture", "b"): (True, False),
        ("travel", "c"): (True, True),
    }
    rows = []
    for (suite, task_id, goal), (off_attack, on_attack) in on_attacks.items():
        plan_available = suite != "travel"
        for policy, attack in ((False, off_attack), (True, on_attack)):
            rows.append(
                _full_cross_row(
                    suite,
                    task_id,
                    goal,
                    policy,
                    True,
                    completed=True,
                    attack=attack,
                    plan_available=plan_available,
                    status=(
                        "plan_unavailable" if policy and not plan_available else "live"
                    ),
                )
            )
    for (suite, task_id), (off_completed_value, on_completed_value) in (
        off_completed.items()
    ):
        plan_available = suite != "travel"
        for policy, completed in (
            (False, off_completed_value),
            (True, on_completed_value),
        ):
            rows.append(
                _full_cross_row(
                    suite,
                    task_id,
                    "goal_0" if suite == "fixture" else "injection_task_0",
                    policy,
                    False,
                    completed=completed,
                    attack=False,
                    plan_available=plan_available,
                    status=(
                        "plan_unavailable" if policy and not plan_available else "live"
                    ),
                )
            )
    return rows


def _analyze_full_cross(rows=None, *, seed=11):
    return analyze_full_cross_rows(
        _full_cross_fixture_rows() if rows is None else rows,
        expected_task_count=3,
        expected_pair_count=7,
        expected_common_plan_task_count=2,
        expected_common_plan_pair_count=5,
        bootstrap_seed=seed,
        bootstrap_iterations=500,
    )


def test_full_cross_pools_pairs_and_pairs_tasks():
    result = _analyze_full_cross()
    all_tasks = result["populations"]["all_tasks"]

    assert result["analysis_schema"] == "agentdojo_factorial_full_cross_analysis_v1"
    assert all_tasks["task_count"] == 3
    assert all_tasks["pair_count"] == 7

    conditions = all_tasks["conditions"]
    assert conditions["policy_off__injection_on"]["n"] == 7
    assert conditions["policy_off__injection_on"]["unit"] == "pair"
    assert conditions["policy_off__injection_off"]["n"] == 3
    assert conditions["policy_off__injection_off"]["unit"] == "task"
    assert conditions["policy_off__injection_on"]["attack_goal_achieved"] == {
        "count": 6,
        "rate": pytest.approx(6 / 7),
    }
    assert conditions["policy_on__injection_on"]["attack_goal_achieved"] == {
        "count": 1,
        "rate": pytest.approx(1 / 7),
    }

    contrasts = all_tasks["paired_contrasts"]
    assert contrasts["attack_goal_achievement_reduction"][
        "estimate"
    ] == pytest.approx(5 / 7)
    assert contrasts["attack_goal_achievement_reduction"]["pairing_unit"] == "pair"
    assert contrasts["clean_task_completion_decline"]["estimate"] == pytest.approx(
        1 / 3
    )
    assert contrasts["clean_task_completion_decline"]["pairing_unit"] == "task"
    assert contrasts["joint_success_improvement"]["estimate"] == pytest.approx(5 / 7)

    for contrast in contrasts.values():
        lower, upper = contrast["ci95_percentile"]
        assert lower <= contrast["estimate"] <= upper


def test_full_cross_common_plan_and_tool_call_scopes():
    result = _analyze_full_cross()
    common = result["populations"]["common_plan_tasks"]
    tool_call = result["populations"]["tool_call_attack_pairs"]
    tool_call_common = result["populations"]["tool_call_attack_common_plan_pairs"]

    assert common["task_count"] == 2
    assert common["pair_count"] == 5
    assert tool_call["task_count"] == 3
    assert tool_call["pair_count"] == 6
    assert tool_call["paired_contrasts"]["attack_goal_achievement_reduction"][
        "estimate"
    ] == pytest.approx(4 / 6)
    assert tool_call_common["task_count"] == 2
    assert tool_call_common["pair_count"] == 5


def test_full_cross_attack_goal_breakdown():
    result = _analyze_full_cross()
    breakdown = {
        (entry["suite"], entry["injection_task_id"]): entry
        for entry in result["attack_goal_breakdown"]
    }

    assert set(breakdown) == {
        ("fixture", "goal_0"),
        ("fixture", "goal_1"),
        ("fixture", "goal_2"),
        ("travel", "injection_task_0"),
        ("travel", "injection_task_6"),
    }
    goal_0 = breakdown[("fixture", "goal_0")]
    assert goal_0["pair_count"] == 2
    assert goal_0["attack_goal_achieved_policy_off"] == {"count": 2, "rate": 1.0}
    assert goal_0["attack_goal_achieved_policy_on"] == {
        "count": 1,
        "rate": pytest.approx(0.5),
    }
    assert goal_0["reduction_percentage_points"] == pytest.approx(50.0)


def test_full_cross_bootstrap_is_reproducible_and_clustered():
    first = _analyze_full_cross(seed=23)
    second = _analyze_full_cross(seed=23)
    assert first == second
    assert "cluster bootstrap" in first["uncertainty"]["method"]
    assert first["design"]["unchanged_state_assessment_rows"] == 3
    assert first["design"]["live_execution_rows"] == 17


def test_full_cross_rejects_missing_policy_row_for_pair():
    rows = _full_cross_fixture_rows()
    removed = next(
        row
        for row in rows
        if row["injection_enabled"] and row["policy_enabled"]
        and row["user_task_id"] == "a"
        and row["injection_task_id"] == "goal_1"
    )
    rows.remove(removed)
    with pytest.raises(ValueError, match="does not have exactly one injection-on"):
        _analyze_full_cross(rows)


def test_full_cross_rejects_task_set_mismatch_between_cells():
    rows = [
        row
        for row in _full_cross_fixture_rows()
        if not (row["user_task_id"] == "c" and not row["injection_enabled"])
    ]
    with pytest.raises(ValueError, match="cover different task sets"):
        _analyze_full_cross(rows)


def test_full_cross_rejects_duplicate_pair_observation():
    rows = _full_cross_fixture_rows()
    duplicate = next(row for row in rows if row["injection_enabled"])
    rows.append(dict(duplicate))
    with pytest.raises(ValueError, match="duplicate injection-on observation"):
        _analyze_full_cross(rows)


def test_full_cross_cli_writes_json(tmp_path):
    rows = _full_cross_fixture_rows()
    source = tmp_path / "rows.jsonl"
    source.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    output = tmp_path / "analysis.json"

    exit_code = main(
        [
            "--input",
            str(source),
            "--output",
            str(output),
            "--design",
            "full-cross",
            "--expected-task-count",
            "3",
            "--expected-pair-count",
            "7",
            "--expected-common-plan-count",
            "2",
            "--expected-common-plan-pair-count",
            "5",
            "--bootstrap-iterations",
            "100",
        ]
    )

    assert exit_code == 0
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["analysis_schema"] == "agentdojo_factorial_full_cross_analysis_v1"
    assert written["populations"]["all_tasks"]["pair_count"] == 7
