"""Regression tests for the deterministic G1/G2 expressibility evaluation."""

from __future__ import annotations

import importlib.metadata

import pytest

from eval.dsl_expressibility import COLLECTION_SCHEMA_SHAPES, evaluate


@pytest.fixture(scope="module")
def result():
    return evaluate()


def test_g2_strictly_expands_the_predefined_case_set(result):
    assert result["totals"]["g1"] == {
        "passed_cases": 11,
        "total_cases": 20,
        "rate": 0.55,
    }
    assert result["totals"]["g2"] == {
        "passed_cases": 20,
        "total_cases": 20,
        "rate": 1.0,
    }
    assert result["delta_percentage_points"] == 45.0


def test_stratified_results_isolate_bounded_for_as_the_difference(result):
    assert result["totals_by_stratum"] == {
        "g1": {
            "baseline_control": {
                "passed_cases": 4,
                "total_cases": 4,
                "rate": 1.0,
            },
            "matched_selection_control": {
                "passed_cases": 7,
                "total_cases": 7,
                "rate": 1.0,
            },
            "single_fanout": {
                "passed_cases": 0,
                "total_cases": 7,
                "rate": 0.0,
            },
            "dependent_nested_fanout": {
                "passed_cases": 0,
                "total_cases": 2,
                "rate": 0.0,
            },
        },
        "g2": {
            "baseline_control": {
                "passed_cases": 4,
                "total_cases": 4,
                "rate": 1.0,
            },
            "matched_selection_control": {
                "passed_cases": 7,
                "total_cases": 7,
                "rate": 1.0,
            },
            "single_fanout": {
                "passed_cases": 7,
                "total_cases": 7,
                "rate": 1.0,
            },
            "dependent_nested_fanout": {
                "passed_cases": 2,
                "total_cases": 2,
                "rate": 1.0,
            },
        },
    }


def test_every_g1_positive_case_remains_expressible_in_g2(result):
    for case in result["cases"]:
        if case["profiles"]["g1"]["expressible"]:
            assert case["profiles"]["g2"]["expressible"]


def test_all_loop_out_of_collection_probes_are_denied(result):
    assert result["loop_invariant_checks"] == {
        "invalid_probes_denied": 45,
        "total_invalid_probes": 45,
    }


def test_g1_fanout_negatives_are_analytic_not_sampled_failures(result):
    fanout_cases = [
        case
        for case in result["cases"]
        if case["stratum"] in {"single_fanout", "dependent_nested_fanout"}
    ]
    assert len(fanout_cases) == 9
    assert {case["case_id"] for case in fanout_cases} >= {
        "single_fanout",
        "dependent_nested_fanout",
    }
    for case in fanout_cases:
        g1 = case["profiles"]["g1"]
        assert g1["classification_basis"] == "analytic_nonexpressibility"
        assert g1["witness_rejected"] is True
        assert g1["representative_states"] == []
        assert "no plan-time fixed length bound" in case["domain"]


def test_representative_states_are_labeled_as_regression_checks(result):
    assert "not proofs over unbounded domains" in result["method"]
    assert result["representative_state_runs"] == {"g1": 36, "g2": 81}
    assert sum(
        len(case["profiles"]["g2"]["representative_states"])
        for case in result["cases"]
    ) == 81


def test_agentdojo_collection_sampling_frame_is_deduplicated(result):
    corpus = result["case_corpus"]
    assert corpus["agentdojo_package_version"] == "0.1.35"
    assert corpus["agentdojo_suite_version"] == "v1"
    assert corpus["source_collection_tool_endpoints"] == 20
    assert corpus["unique_collection_schema_shapes"] == 7
    assert corpus["case_counts_by_stratum"] == {
        "baseline_control": 4,
        "matched_selection_control": 7,
        "single_fanout": 7,
        "dependent_nested_fanout": 2,
    }
    assert len(COLLECTION_SCHEMA_SHAPES) == 7
    assert len({shape.return_schema for shape in COLLECTION_SCHEMA_SHAPES}) == 7
    assert sum(len(shape.source_tools) for shape in COLLECTION_SCHEMA_SHAPES) == 20


def test_declared_schema_provenance_matches_the_local_agentdojo_adapter():
    from benchmarks.agentdojo_adapter import AGENTDOJO_SUITES, load_suite

    assert importlib.metadata.version("agentdojo") == "0.1.35"
    observed: dict[str, set[str]] = {}
    for suite_name in AGENTDOJO_SUITES:
        suite = load_suite(suite_name)
        for tool_name, tool in suite.tools.items():
            return_schema = tool.doc.returns
            if return_schema.startswith("list"):
                observed.setdefault(return_schema, set()).add(
                    f"{suite_name}.{tool_name}"
                )
    declared = {
        shape.return_schema: set(shape.source_tools)
        for shape in COLLECTION_SCHEMA_SHAPES
    }
    assert observed == declared


def test_each_schema_shape_has_matched_and_single_fanout_cases(result):
    schema_cases = [
        case
        for case in result["cases"]
        if case["provenance"]["kind"]
        == "agentdojo_v1_collection_schema_shape"
    ]
    by_role = {
        role: {
            case["provenance"]["shape_id"]
            for case in schema_cases
            if case["provenance"]["case_role"] == role
        }
        for role in (
            "matched_selection_control",
            "single_fanout",
            "dependent_nested_fanout",
        )
    }
    all_shapes = {shape.shape_id for shape in COLLECTION_SCHEMA_SHAPES}
    nested_shapes = {
        shape.shape_id
        for shape in COLLECTION_SCHEMA_SHAPES
        if shape.nested_field is not None
    }
    assert by_role["matched_selection_control"] == all_shapes
    assert by_role["single_fanout"] == all_shapes
    assert by_role["dependent_nested_fanout"] == nested_shapes
