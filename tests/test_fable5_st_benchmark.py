from __future__ import annotations

from types import SimpleNamespace

import pytest

from eval.fable5_st_benchmark import (
    ARMS,
    MODEL,
    BenchmarkCase,
    BenchmarkContractError,
    _execution_order,
    _mcnemar,
    _parse_args,
    _prepare_run_dir,
    _revision_prompt,
    run_benchmark,
)
from eval.funnel import Corpus, Task
from eval.metrics import REF_EXACT_AUTHORIZATION
from pauth.codegen import ToolDoc, _cost


def _metric_row(task_key: str, passed: bool) -> dict:
    return {
        "task_key": task_key,
        "metrics": {
            REF_EXACT_AUTHORIZATION: "pass" if passed else "fail",
        },
    }


def _fake_cases(n: int = 97) -> list[BenchmarkCase]:
    suite = SimpleNamespace(tool_docs=lambda: [])
    corpus = Corpus("agentdojo:banking", suite, [], adj=None)
    return [
        BenchmarkCase(
            suite_name="banking",
            corpus=corpus,
            task=Task(
                task_id=f"user_task_{index}",
                prompt=f"task {index}",
                plan_code=None,
                injections=[],
            ),
        )
        for index in range(n)
    ]


def test_model_and_arms_are_fixed() -> None:
    assert MODEL == "claude-fable-5"
    assert ARMS == ("direct1", "st", "direct2-revise")
    assert _parse_args(["--run-dir", "/tmp/x", "--model", MODEL]).model == MODEL
    with pytest.raises(SystemExit):
        _parse_args(["--run-dir", "/tmp/x", "--model", "claude-other"])


def test_unknown_fable_pricing_is_not_faked_with_gpt_rates() -> None:
    assert _cost(MODEL, 1_000, 100) is None
    assert _cost("gpt-4.1", 1_000, 100) == pytest.approx(0.0028)


def test_arm_order_counterbalances_primary_and_keeps_revision_after_direct() -> None:
    assert _execution_order(0) == ("direct1", "st", "direct2-revise")
    assert _execution_order(1) == ("st", "direct1", "direct2-revise")


def test_revision_prompt_contains_task_tools_and_exact_draft() -> None:
    tool = ToolDoc(
        name="send_message",
        description="send one message",
        parameters=[
            {"name": "recipient", "type": "string"},
            {"name": "body", "type": "string"},
        ],
        returns="object",
    )
    draft = 'def run():\n    send_message("a", "b")\n'
    prompt = _revision_prompt("send b to a", [tool], draft)
    assert "send b to a" in prompt
    assert "send_message" in prompt
    assert draft in prompt
    assert "FIRST DRAFT" in prompt


def test_fresh_directory_and_resume_are_two_distinct_states(tmp_path) -> None:
    run_dir = tmp_path / "run"
    contract = {"schema_version": 1, "task_count": 97}
    created = _prepare_run_dir(run_dir, contract, resume=False)
    assert created["contract"] == contract
    assert _prepare_run_dir(run_dir, contract, resume=True) == created
    with pytest.raises(BenchmarkContractError, match="must not already exist"):
        _prepare_run_dir(run_dir, contract, resume=False)
    with pytest.raises(BenchmarkContractError, match="does not match"):
        _prepare_run_dir(run_dir, {"task_count": 96}, resume=True)


def test_dry_run_makes_no_directory_or_client(monkeypatch, tmp_path) -> None:
    target = tmp_path / "fresh"
    monkeypatch.setattr(
        "eval.fable5_st_benchmark.load_cases", lambda: _fake_cases()
    )
    monkeypatch.setattr(
        "eval.fable5_st_benchmark._make_client",
        lambda: pytest.fail("dry-run must not construct an API client"),
    )
    result = run_benchmark(target, dry_run=True)
    assert result["corpus_task_count"] == 97
    assert result["expected_result_rows"] == 97 * 3
    assert result["api_calls_made"] == 0
    assert not target.exists()


def test_exact_mcnemar_uses_paired_discordant_tasks() -> None:
    left = {
        "a": _metric_row("a", True),
        "b": _metric_row("b", True),
        "c": _metric_row("c", False),
        "d": _metric_row("d", False),
    }
    right = {
        "a": _metric_row("a", False),
        "b": _metric_row("b", True),
        "c": _metric_row("c", True),
        "d": _metric_row("d", True),
    }
    result = _mcnemar(left, right)
    assert result["paired_n"] == 4
    assert result["left_only_pass"] == 1
    assert result["right_only_pass"] == 2
    assert 0 <= result["exact_mcnemar_p_two_sided"] <= 1
