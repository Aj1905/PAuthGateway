"""Regression tests for the deterministic G1/G2 expressibility evaluation."""

from __future__ import annotations

from eval.dsl_expressibility import evaluate


def test_g2_strictly_expands_the_predefined_case_set():
    result = evaluate()
    assert result["totals"]["g1"]["passed_cases"] == 4
    assert result["totals"]["g1"]["total_cases"] == 6
    assert result["totals"]["g2"]["passed_cases"] == 6
    assert result["totals"]["g2"]["total_cases"] == 6
    assert result["delta_percentage_points"] == 100 / 3


def test_every_g1_positive_case_remains_expressible_in_g2():
    result = evaluate()
    for case in result["cases"]:
        if case["profiles"]["g1"]["expressible"]:
            assert case["profiles"]["g2"]["expressible"]


def test_all_loop_out_of_collection_probes_are_denied():
    checks = evaluate()["loop_invariant_checks"]
    assert checks == {"invalid_probes_denied": 7, "total_invalid_probes": 7}


def test_g1_fanout_negatives_are_marked_as_analytic_not_sampled_failures():
    result = evaluate()
    fanout_cases = {
        case["case_id"]: case
        for case in result["cases"]
        if case["case_id"] in {"single_fanout", "dependent_nested_fanout"}
    }
    assert set(fanout_cases) == {"single_fanout", "dependent_nested_fanout"}
    for case in fanout_cases.values():
        g1 = case["profiles"]["g1"]
        assert g1["classification_basis"] == "analytic_nonexpressibility"
        assert g1["witness_rejected"] is True
        assert g1["representative_states"] == []
        assert "no plan-time fixed" in case["domain"]


def test_representative_states_are_labeled_as_regression_checks():
    result = evaluate()
    assert "not proofs over unbounded domains" in result["method"]
    assert sum(
        len(case["profiles"]["g2"]["representative_states"])
        for case in result["cases"]
    ) == 15
