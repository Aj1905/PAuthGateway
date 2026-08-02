"""Tests for the deterministic AgentDojo reference-trace diagnostic."""

from __future__ import annotations

import json

import pytest

from eval.agentdojo_reference_trace import evaluate


@pytest.fixture(scope="module")
def result():
    return evaluate()


def test_corpus_and_results_are_pinned(result):
    assert result["agentdojo"] == {
        "package_version": "0.1.35",
        "benchmark_version": "v1",
        "suite_task_counts": {
            "banking": 16,
            "slack": 21,
            "travel": 20,
            "workspace": 40,
        },
        "tasks": 97,
        "reference_tool_calls": 339,
        "suite_reference_tool_calls": {
            "banking": 33,
            "slack": 98,
            "travel": 124,
            "workspace": 84,
        },
    }
    assert result["profiles"]["g1"]["passed"] == 96
    assert result["profiles"]["g1"]["total"] == 97
    assert result["profiles"]["g2"]["passed"] == 97
    assert result["profiles"]["g2"]["total"] == 97


def test_only_gain_is_workspace_attachment_dict_literal(result):
    assert result["g2_gain_tasks"] == 1
    assert result["g2_loss_tasks"] == []
    assert result["difference"] == [
        {
            "task_id": "workspace.user_task_33",
            "cause": "dict_literal",
            "g1_dsl_rejection": "dict literals are not in the DSL [G1]",
        }
    ]
    assert result["bounded_for_gain"] == 0


def test_every_accepted_program_runs_and_replays_the_reference_trace(result):
    for task in result["tasks"]:
        for profile in ("g1", "g2"):
            row = task["profiles"][profile]
            if not row["dsl_accepted"]:
                assert task["task_id"] == "workspace.user_task_33"
                assert profile == "g1"
                continue
            assert row["pipeline_executed"] is True
            assert row["pipeline_ok"] is True
            assert row["reference_trace_exact"] is True
            assert row["observed_tool_calls"] == task["reference_tool_calls"]
            assert row["observed_trace_sha256"] == task["reference_trace_sha256"]


def test_parameter_holes_are_filled_from_real_agentdojo_defaults(result):
    task = next(
        row for row in result["tasks"] if row["task_id"] == "banking.user_task_12"
    )
    assert {
        "tool_call_index": 2,
        "tool": "update_scheduled_transaction",
        "parameter": "recipient",
        "rendered_default": "None",
    } in task["filled_parameter_defaults"]


def test_agentdojo_ground_truth_self_check_failure_is_disclosed(result):
    check = result["agentdojo_suite_self_check"]
    assert check["check_injectable"] is False
    assert check["passed"] == 96
    assert check["total"] == 97
    assert check["failed_tasks"] == [
        {
            "task_id": "workspace.user_task_7",
            "reason": "Ground truth does not solve the task",
        }
    ]


def test_diagnostic_is_not_mislabeled_as_feasibility(result):
    assert result["evaluation_name"] == "AgentDojo v1 reference-trace encodability"
    assert result["not_feasibility_expressible"] is True
    assert "FEASIBILITY_EXPRESSIBLE" not in json.dumps(result)
