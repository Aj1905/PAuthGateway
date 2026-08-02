"""Regression tests for the pinned AgentDojo fan-out audit."""

from __future__ import annotations

import json
import re

from eval.agentdojo_fanout_audit import (
    AGENTDOJO_PACKAGE_VERSION,
    AGENTDOJO_SUITE_VERSION,
    EXPECTED_CORPUS_SHA256,
    EXPECTED_REFERENCE_TOOL_CALLS,
    EXPECTED_SUITE_COUNTS,
    build_audit,
    main,
)


EXPECTED_CANDIDATES = {
    "workspace.user_task_25",
    "slack.user_task_5",
    "slack.user_task_8",
    "slack.user_task_9",
    "slack.user_task_10",
    "slack.user_task_13",
    "slack.user_task_14",
    "slack.user_task_15",
    "slack.user_task_18",
    "slack.user_task_19",
    "slack.user_task_20",
}


def _rows_by_key() -> dict[str, dict]:
    return {row["task_key"]: row for row in build_audit()["tasks"]}


def test_audit_covers_the_pinned_97_task_corpus() -> None:
    result = build_audit()
    assert result["contract"]["agentdojo_package_version"] == AGENTDOJO_PACKAGE_VERSION
    assert result["contract"]["agentdojo_suite_version"] == AGENTDOJO_SUITE_VERSION
    assert result["contract"]["expected_suite_counts"] == EXPECTED_SUITE_COUNTS
    assert (
        result["contract"]["expected_reference_tool_calls"]
        == EXPECTED_REFERENCE_TOOL_CALLS
        == 339
    )
    assert len(result["tasks"]) == 97
    assert len({row["task_key"] for row in result["tasks"]}) == 97
    assert result["contract"]["corpus_sha256"] == EXPECTED_CORPUS_SHA256
    assert re.fullmatch(r"[0-9a-f]{64}", EXPECTED_CORPUS_SHA256)

    for row in result["tasks"]:
        assert row["reference_trace_length"] == len(row["reference_trace"])
        assert re.fullmatch(r"[0-9a-f]{64}", row["task_sha256"])
        assert re.fullmatch(r"[0-9a-f]{64}", row["reference_trace_sha256"])
        assert re.fullmatch(
            r"[0-9a-f]{64}", row["reference_positional_trace_sha256"]
        )
        assert re.fullmatch(r"[0-9a-f]{64}", row["reference_run_sha256"])


def test_candidate_set_and_current_g2_blockers_are_complete() -> None:
    result = build_audit()
    assert "FEASIBILITY_EXPRESSIBLE" not in json.dumps(result)
    rows = _rows_by_key()
    observed = {
        key
        for key, row in rows.items()
        if row["fanout_audit"][
            "runtime_cardinality_dependent_individual_calls"
        ]
    }
    assert observed == EXPECTED_CANDIDATES
    findings = result["fanout_semantic_findings"]
    assert findings["candidate_tasks"] == 11
    assert findings["non_candidate_tasks"] == 86
    assert findings["partial_bounded_for_prefixes"] == 8
    assert findings["full_task_g2_only_witnesses"] == 0
    assert findings["full_task_g2_only_witness_denominator"] == 97
    assert findings["bounded_for_empirical_gain_tasks"] == 0

    for key in EXPECTED_CANDIDATES:
        audit = rows[key]["fanout_audit"]
        assert audit["status"] == "candidate_blocked_in_current_g2"
        assert audit["no_bulk_equivalent"] is True
        assert audit["full_task_g2_only_witness"] is False
        assert audit["g2_blocker_category"]
        assert audit["g2_blocker_rationale"]

    for key, row in rows.items():
        if key in EXPECTED_CANDIDATES:
            continue
        audit = row["fanout_audit"]
        assert audit["status"] == "not_candidate"
        assert audit["non_candidate_reason_class"]
        assert audit["g2_blocker_category"] is None


def test_fixed_trace_check_separates_dict_literal_from_bounded_for() -> None:
    result = build_audit()
    check = result["fixed_reference_trace_check"]
    assert check["g1_accepted"] == 96
    assert check["g2_accepted"] == 97
    assert check["g2_only_tasks"] == ["workspace.user_task_33"]
    assert check["g2_only_feature_by_task"] == {
        "workspace.user_task_33": "dict_literal"
    }
    assert check["bounded_for_contribution_tasks"] == []
    assert all(not row["reference_run_contains_for"] for row in result["tasks"])

    row = _rows_by_key()["workspace.user_task_33"]
    assert row["fixed_trace_profile_acceptance"]["g1"]["accepted"] is False
    assert "dict literals" in row["fixed_trace_profile_acceptance"]["g1"][
        "rejection"
    ]
    assert row["fixed_trace_profile_acceptance"]["g2"]["accepted"] is True


def test_known_reference_trace_caveats_remain_visible() -> None:
    rows = _rows_by_key()
    assert "External_0" in rows["slack.user_task_5"]["fanout_audit"][
        "reference_trace_caveat"
    ]
    for key in ("slack.user_task_9", "slack.user_task_10"):
        assert "get_users_in_channel" in rows[key]["fanout_audit"][
            "reference_trace_caveat"
        ]


def test_cli_writes_the_same_deterministic_payload(tmp_path) -> None:
    output = tmp_path / "audit.json"
    assert main(["--output", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8")) == build_audit()
