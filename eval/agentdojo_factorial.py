"""Matched 2x2 AgentDojo evaluation for PAuthGateway.

The four conditions cross authorization-policy application with indirect
prompt-injection insertion.  All conditions use the same 97 user tasks and the
same assigned injection goal per task.  The 82 assignments used by
``agentdojo_live_injection`` are frozen exactly; the remaining 15 tasks receive
a stable hash-based assignment.

Examples::

    # Inspect the complete 4 x 97 design without API calls.
    .venv/bin/python -m eval.agentdojo_factorial --dry-run

    # Smoke-test every condition on one task.
    .venv/bin/python -m eval.agentdojo_factorial --limit 1

    # Run only one condition.
    .venv/bin/python -m eval.agentdojo_factorial \
        --condition policy_off_injection_off

    # Run every user task x injection goal pair (629 per injection-on cell).
    .venv/bin/python -m eval.agentdojo_factorial --pairs full
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import time
from ast import literal_eval
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import openai
from dotenv import load_dotenv

from agentdojo.agent_pipeline.base_pipeline_element import BasePipelineElement
from agentdojo.agent_pipeline.tool_execution import is_string_list, tool_result_to_str
from agentdojo.attacks import load_attack
from agentdojo.functions_runtime import EmptyEnv, Env, FunctionCall, FunctionsRuntime
from agentdojo.task_suite.load_suites import get_suites
from agentdojo.task_suite.task_suite import model_output_from_messages
from agentdojo.types import (
    ChatMessage,
    ChatToolResultMessage,
    text_content_block_from_string,
)

from benchmarks.agentdojo_adapter import load_suite
from benchmarks.structured_read import augment_with_structuring
from eval.agentdojo_live_injection import (
    DEFAULT_PLAN_MODEL,
    DEFAULT_PLAN_ROOT,
    DEFAULT_RESULT_ROOT,
    SUITES,
    InstrumentedOpenAILLM,
    SelectedPlan,
    _make_pipeline,
    build_evaluation_cases,
    injection_text_present,
    select_valid_plans,
)
from eval.metrics import (
    OUTCOME_ATTACK_GOAL_ACHIEVED,
    OUTCOME_TASK_COMPLETED,
    OUTCOME_TASK_COMPLETED_WITHOUT_ATTACK_GOAL,
)
from pauth.enforcer import Decision, Enforcer
from pauth.envelope import EnvelopeStore, KeyRing
from pauth.evaluator import wrap


@dataclasses.dataclass(frozen=True)
class Condition:
    """One cell of the policy x injection factorial design."""

    policy_enabled: bool
    injection_enabled: bool

    @property
    def name(self) -> str:
        policy = "on" if self.policy_enabled else "off"
        injection = "on" if self.injection_enabled else "off"
        return f"policy_{policy}_injection_{injection}"


CONDITIONS = (
    Condition(False, False),
    Condition(False, True),
    Condition(True, False),
    Condition(True, True),
)
CONDITION_BY_NAME = {condition.name: condition for condition in CONDITIONS}


@dataclasses.dataclass(frozen=True)
class TaskCase:
    """One user task, its paired injection goal, and an optional cached plan."""

    suite_name: str
    user_task_id: str
    injection_task_id: str
    plan: SelectedPlan | None

    @property
    def task_key(self) -> tuple[str, str]:
        return self.suite_name, self.user_task_id


@dataclasses.dataclass(frozen=True)
class ToolEvent:
    tool: str
    args: dict[str, Any]
    policy_enabled: bool
    policy_permitted: bool | None
    reason: str
    executed: bool
    error: str | None
    injection_seen_before: bool


def _numeric_task_ids(task_ids: Sequence[str]) -> list[str]:
    return sorted(task_ids, key=lambda task_id: int(task_id.rsplit("_", 1)[-1]))


def _missing_assignment(
    suite_name: str,
    user_task_id: str,
    injection_task_ids: Sequence[str],
) -> str:
    """Assign a missing-plan task reproducibly without shifting frozen pairs."""

    digest = hashlib.sha256(f"{suite_name}/{user_task_id}".encode()).digest()
    index = int.from_bytes(digest[:8], "big") % len(injection_task_ids)
    return injection_task_ids[index]


def build_full_cross_cases(
    plans: Sequence[SelectedPlan],
    suites: Sequence[str] = SUITES,
) -> list[TaskCase]:
    """Build every user task x injection goal pair (629 for the four suites)."""

    plan_by_task = {(plan.suite_name, plan.task_id): plan for plan in plans}
    cases: list[TaskCase] = []
    for suite_name in suites:
        suite = get_suites("v1")[suite_name]
        injection_task_ids = _numeric_task_ids(tuple(suite.injection_tasks))
        for user_task_id in _numeric_task_ids(tuple(suite.user_tasks)):
            for injection_task_id in injection_task_ids:
                cases.append(
                    TaskCase(
                        suite_name=suite_name,
                        user_task_id=user_task_id,
                        injection_task_id=injection_task_id,
                        plan=plan_by_task.get((suite_name, user_task_id)),
                    )
                )
    return cases


def build_task_cases(
    plans: Sequence[SelectedPlan],
    suites: Sequence[str] = SUITES,
) -> list[TaskCase]:
    """Build the 97-task population while preserving the old 82 assignments."""

    plan_by_task = {(plan.suite_name, plan.task_id): plan for plan in plans}
    frozen_pairs = {
        (case.plan.suite_name, case.plan.task_id): case.injection_task_id
        for case in build_evaluation_cases(plans, "round-robin")
    }

    cases: list[TaskCase] = []
    for suite_name in suites:
        suite = get_suites("v1")[suite_name]
        injection_task_ids = _numeric_task_ids(tuple(suite.injection_tasks))
        for user_task_id in _numeric_task_ids(tuple(suite.user_tasks)):
            task_key = (suite_name, user_task_id)
            injection_task_id = frozen_pairs.get(task_key)
            if injection_task_id is None:
                injection_task_id = _missing_assignment(
                    suite_name,
                    user_task_id,
                    injection_task_ids,
                )
            cases.append(
                TaskCase(
                    suite_name=suite_name,
                    user_task_id=user_task_id,
                    injection_task_id=injection_task_id,
                    plan=plan_by_task.get(task_key),
                )
            )
    return cases


class InstrumentedToolsExecutor(BasePipelineElement):
    """Execute calls in both cells, optionally checking an Enforcer first."""

    def __init__(
        self,
        enforcer: Enforcer | None,
        injected_values: Sequence[str],
        *,
        policy_enabled: bool,
    ) -> None:
        if policy_enabled and enforcer is None:
            raise ValueError("policy_enabled requires an Enforcer")
        self.enforcer = enforcer
        self.policy_enabled = policy_enabled
        self.injected_values = tuple(injected_values)
        self.events: list[ToolEvent] = []
        self.executed_calls: list[FunctionCall] = []
        self.tool_outputs: list[str] = []

    @property
    def saw_injection(self) -> bool:
        return injection_text_present(self.injected_values, self.tool_outputs)

    @staticmethod
    def _positional_args(runtime: FunctionsRuntime, call: FunctionCall) -> list[Any]:
        function = runtime.functions.get(call.function)
        if function is None:
            return list(call.args.values())
        return [
            call.args[name]
            for name in function.parameters.model_fields
            if name in call.args
        ]

    @staticmethod
    def _tool_result(
        call: FunctionCall,
        content: str,
        error: str | None,
    ) -> ChatToolResultMessage:
        return ChatToolResultMessage(
            role="tool",
            content=[text_content_block_from_string(content)],
            tool_call_id=call.id,
            tool_call=call,
            error=error,
        )

    def query(
        self,
        query: str,
        runtime: FunctionsRuntime,
        env: Env = EmptyEnv(),
        messages: Sequence[ChatMessage] = (),
        extra_args: dict = {},
    ) -> tuple[str, FunctionsRuntime, Env, Sequence[ChatMessage], dict]:
        if not messages or messages[-1]["role"] != "assistant":
            return query, runtime, env, messages, extra_args
        tool_calls = messages[-1]["tool_calls"]
        if not tool_calls:
            return query, runtime, env, messages, extra_args

        results: list[ChatToolResultMessage] = []
        for call in tool_calls:
            injection_seen_before = self.saw_injection
            if call.function not in runtime.functions:
                error = f"ToolNotFoundError: `{call.function}` is not available."
                self.events.append(
                    ToolEvent(
                        tool=call.function,
                        args=dict(call.args),
                        policy_enabled=self.policy_enabled,
                        policy_permitted=None,
                        reason="tool is not available",
                        executed=False,
                        error=error,
                        injection_seen_before=injection_seen_before,
                    )
                )
                results.append(self._tool_result(call, "", error))
                continue

            for key, value in list(call.args.items()):
                if isinstance(value, str) and is_string_list(value):
                    call.args[key] = literal_eval(value)

            decision: Decision | None = None
            if self.policy_enabled:
                assert self.enforcer is not None
                decision = self.enforcer.check(
                    call.function,
                    self._positional_args(runtime, call),
                )
                if not decision.permit:
                    error = f"PAuthDenied: {decision.reason}"
                    self.events.append(
                        ToolEvent(
                            tool=call.function,
                            args=dict(call.args),
                            policy_enabled=True,
                            policy_permitted=False,
                            reason=decision.reason,
                            executed=False,
                            error=error,
                            injection_seen_before=injection_seen_before,
                        )
                    )
                    results.append(self._tool_result(call, "", error))
                    continue

            result, error = runtime.run_function(env, call.function, call.args)
            reason = (
                decision.reason
                if decision is not None
                else "authorization policy disabled"
            )
            if error is not None:
                self.events.append(
                    ToolEvent(
                        tool=call.function,
                        args=dict(call.args),
                        policy_enabled=self.policy_enabled,
                        policy_permitted=True if self.policy_enabled else None,
                        reason=reason,
                        executed=False,
                        error=error,
                        injection_seen_before=injection_seen_before,
                    )
                )
                results.append(self._tool_result(call, "", error))
                continue

            formatted = tool_result_to_str(result)
            self.tool_outputs.append(formatted)
            self.executed_calls.append(
                FunctionCall(function=call.function, args=dict(call.args), id=call.id)
            )
            if decision is not None:
                assert self.enforcer is not None and decision.rule is not None
                self.enforcer.record(decision.rule, wrap(result))
            self.events.append(
                ToolEvent(
                    tool=call.function,
                    args=dict(call.args),
                    policy_enabled=self.policy_enabled,
                    policy_permitted=True if self.policy_enabled else None,
                    reason=reason,
                    executed=True,
                    error=None,
                    injection_seen_before=injection_seen_before,
                )
            )
            results.append(self._tool_result(call, formatted, None))

        return query, runtime, env, [*messages, *results], extra_args


def _base_row(case: TaskCase, condition: Condition, attack_name: str) -> dict[str, Any]:
    return {
        "suite": case.suite_name,
        "user_task_id": case.user_task_id,
        "injection_task_id": case.injection_task_id,
        "attack": attack_name,
        "policy_enabled": condition.policy_enabled,
        "injection_enabled": condition.injection_enabled,
        "condition": condition.name,
        "plan_available": case.plan is not None,
        "plan_path": str(case.plan.path) if case.plan is not None else None,
    }


def _injections(
    case: TaskCase,
    condition: Condition,
    llm: InstrumentedOpenAILLM,
    attack_name: str,
    max_iters: int,
) -> dict[str, str]:
    if not condition.injection_enabled:
        return {}
    suite = get_suites("v1")[case.suite_name]
    user_task = suite.get_user_task_by_id(case.user_task_id)
    injection_task = suite.get_injection_task_by_id(case.injection_task_id)
    placeholder = InstrumentedToolsExecutor(None, (), policy_enabled=False)
    pipeline = _make_pipeline(llm, placeholder, max_iters)
    return load_attack(attack_name, suite, pipeline).attack(user_task, injection_task)


def _plan_unavailable_row(
    case: TaskCase,
    condition: Condition,
    llm: InstrumentedOpenAILLM,
    attack_name: str,
    max_iters: int,
) -> dict[str, Any]:
    """Score unchanged state; do not invoke the live agent."""

    assert condition.policy_enabled and case.plan is None
    suite = get_suites("v1")[case.suite_name]
    user_task = suite.get_user_task_by_id(case.user_task_id)
    injections = _injections(case, condition, llm, attack_name, max_iters)
    task_environment = user_task.init_environment(
        suite.load_and_inject_default_environment(injections)
    )
    pre_environment = task_environment.model_copy(deep=True)
    post_environment = task_environment.model_copy(deep=True)

    attack_goal_achieved: bool | None = None
    if condition.injection_enabled:
        injection_task = suite.get_injection_task_by_id(case.injection_task_id)
        attack_goal_achieved = bool(
            suite._check_injection_task_security(
                injection_task,
                [],
                pre_environment,
                post_environment,
                [],
            )
        )
    attack_goal_for_row = bool(attack_goal_achieved)
    joint = False
    return {
        **_base_row(case, condition, attack_name),
        "status": "plan_unavailable",
        "execution_status": "plan_unavailable",
        "injection_seen": False,
        "task_completed": False,
        "attack_goal_achieved": attack_goal_for_row,
        "task_completed_without_attack_goal": joint,
        OUTCOME_TASK_COMPLETED: False,
        OUTCOME_ATTACK_GOAL_ACHIEVED: attack_goal_for_row,
        OUTCOME_TASK_COMPLETED_WITHOUT_ATTACK_GOAL: joint,
        "policy_denied_calls": 0,
        "policy_permitted_calls": 0,
        "executed_calls": 0,
        "llm_requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "elapsed_seconds": 0.0,
        "events": [],
    }


def run_case(
    case: TaskCase,
    condition: Condition,
    llm: InstrumentedOpenAILLM,
    attack_name: str,
    max_iters: int,
) -> dict[str, Any]:
    """Run one matched case, or score a policy-on missing-plan case offline."""

    if condition.policy_enabled and case.plan is None:
        return _plan_unavailable_row(case, condition, llm, attack_name, max_iters)

    suite = get_suites("v1")[case.suite_name]
    user_task = suite.get_user_task_by_id(case.user_task_id)
    enforcer: Enforcer | None = None
    if condition.policy_enabled:
        assert case.plan is not None
        spec = augment_with_structuring(load_suite(case.suite_name))
        enforcer = Enforcer(
            case.plan.prepared.rules,
            EnvelopeStore(KeyRing()),
            spec.tool_signer(),
        )

    injections = _injections(case, condition, llm, attack_name, max_iters)
    executor = InstrumentedToolsExecutor(
        enforcer,
        tuple(injections.values()),
        policy_enabled=condition.policy_enabled,
    )
    pipeline = _make_pipeline(llm, executor, max_iters)
    task_environment = user_task.init_environment(
        suite.load_and_inject_default_environment(injections)
    )
    pre_environment = task_environment.model_copy(deep=True)
    runtime = FunctionsRuntime(suite.tools)

    before_requests = llm.requests
    before_input = llm.input_tokens
    before_output = llm.output_tokens
    started = time.perf_counter()
    _, _, post_environment, messages, _ = pipeline.query(
        user_task.PROMPT,
        runtime,
        task_environment,
    )
    elapsed = time.perf_counter() - started

    model_output = model_output_from_messages(messages) or []
    task_completed = bool(
        suite._check_user_task_utility(
            user_task,
            model_output,
            pre_environment,
            post_environment,
            executor.executed_calls,
        )
    )
    attack_goal_achieved: bool | None = None
    if condition.injection_enabled:
        injection_task = suite.get_injection_task_by_id(case.injection_task_id)
        attack_goal_achieved = bool(
            suite._check_injection_task_security(
                injection_task,
                model_output,
                pre_environment,
                post_environment,
                executor.executed_calls,
            )
        )
    attack_goal_for_row = bool(attack_goal_achieved)
    joint = task_completed and not attack_goal_for_row
    return {
        **_base_row(case, condition, attack_name),
        "status": "live",
        "execution_status": "live",
        "injection_seen": executor.saw_injection,
        "task_completed": task_completed,
        "attack_goal_achieved": attack_goal_for_row,
        "task_completed_without_attack_goal": joint,
        OUTCOME_TASK_COMPLETED: task_completed,
        OUTCOME_ATTACK_GOAL_ACHIEVED: attack_goal_for_row,
        OUTCOME_TASK_COMPLETED_WITHOUT_ATTACK_GOAL: joint,
        "policy_denied_calls": sum(
            event.policy_permitted is False for event in executor.events
        ),
        "policy_permitted_calls": sum(
            event.policy_permitted is True for event in executor.events
        ),
        "executed_calls": len(executor.executed_calls),
        "llm_requests": llm.requests - before_requests,
        "input_tokens": llm.input_tokens - before_input,
        "output_tokens": llm.output_tokens - before_output,
        "elapsed_seconds": elapsed,
        "events": [dataclasses.asdict(event) for event in executor.events],
    }


def row_key(row: Mapping[str, Any]) -> tuple[bool, bool, str, str, str]:
    """Analyzer contract: condition key followed by the task and goal keys."""

    return (
        bool(row["policy_enabled"]),
        bool(row["injection_enabled"]),
        str(row["suite"]),
        str(row["user_task_id"]),
        str(row["injection_task_id"]),
    )


def case_key(
    case: TaskCase, condition: Condition
) -> tuple[bool, bool, str, str, str]:
    return (
        condition.policy_enabled,
        condition.injection_enabled,
        case.suite_name,
        case.user_task_id,
        case.injection_task_id,
    )


def _population_summary(
    rows: Sequence[Mapping[str, Any]],
    condition: Condition,
    expected: int,
) -> dict[str, Any]:
    errors = [row for row in rows if "error" in row]
    complete = len(rows) == expected and not errors
    task_completed = sum(row.get(OUTCOME_TASK_COMPLETED) is True for row in rows)
    attack_goal = (
        sum(row.get(OUTCOME_ATTACK_GOAL_ACHIEVED) is True for row in rows)
        if condition.injection_enabled
        else None
    )
    joint = (
        sum(
            row.get(OUTCOME_TASK_COMPLETED_WITHOUT_ATTACK_GOAL) is True
            for row in rows
        )
        if condition.injection_enabled
        else None
    )
    return {
        "expected_denominator": expected,
        "rows": len(rows),
        "complete": complete,
        "errors": len(errors),
        OUTCOME_TASK_COMPLETED: {
            "count": task_completed,
            "rate": task_completed / expected if complete and expected else None,
        },
        OUTCOME_ATTACK_GOAL_ACHIEVED: (
            {
                "count": attack_goal,
                "rate": attack_goal / expected if complete and expected else None,
            }
            if attack_goal is not None
            else None
        ),
        OUTCOME_TASK_COMPLETED_WITHOUT_ATTACK_GOAL: (
            {
                "count": joint,
                "rate": joint / expected if complete and expected else None,
            }
            if joint is not None
            else None
        ),
    }


def summarize(
    rows: Sequence[Mapping[str, Any]],
    task_cases: Sequence[TaskCase],
    conditions: Sequence[Condition],
) -> dict[str, Any]:
    condition_summaries: dict[str, Any] = {}
    expected_all = len(task_cases)
    expected_common = sum(case.plan is not None for case in task_cases)
    for condition in conditions:
        condition_rows = [
            row
            for row in rows
            if bool(row["policy_enabled"]) == condition.policy_enabled
            and bool(row["injection_enabled"]) == condition.injection_enabled
        ]
        common_rows = [row for row in condition_rows if bool(row["plan_available"])]
        condition_summaries[condition.name] = {
            "policy_enabled": condition.policy_enabled,
            "injection_enabled": condition.injection_enabled,
            "population_97": _population_summary(
                condition_rows,
                condition,
                expected_all,
            ),
            "common_82": _population_summary(
                common_rows,
                condition,
                expected_common,
            ),
            "live_agent_cases": sum(
                row.get("execution_status") == "live" for row in condition_rows
            ),
            "plan_unavailable_cases": sum(
                row.get("execution_status") == "plan_unavailable"
                for row in condition_rows
            ),
            "injection_seen": (
                sum(bool(row.get("injection_seen")) for row in condition_rows)
                if condition.injection_enabled
                else None
            ),
            "policy_denied_calls": (
                sum(int(row.get("policy_denied_calls", 0)) for row in condition_rows)
                if condition.policy_enabled
                else None
            ),
            "executed_calls": sum(
                int(row.get("executed_calls", 0)) for row in condition_rows
            ),
            "llm_requests": sum(
                int(row.get("llm_requests", 0)) for row in condition_rows
            ),
            "input_tokens": sum(
                int(row.get("input_tokens", 0)) for row in condition_rows
            ),
            "output_tokens": sum(
                int(row.get("output_tokens", 0)) for row in condition_rows
            ),
            "elapsed_seconds": sum(
                float(row.get("elapsed_seconds", 0.0)) for row in condition_rows
            ),
        }
    return {
        "protocol": "matched AgentDojo policy x injection factorial evaluation",
        "task_key": ["suite", "user_task_id"],
        "condition_key": ["policy_enabled", "injection_enabled"],
        "natural_language_tasks": expected_all,
        "common_valid_plan_tasks": expected_common,
        "conditions": condition_summaries,
    }


def _default_output(model: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_model = model.replace("/", "_").replace(".", "_")
    return DEFAULT_RESULT_ROOT / f"agentdojo_factorial_{safe_model}_{stamp}.jsonl"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _command_output(*args: str) -> str | None:
    try:
        result = subprocess.run(
            args,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip()


def _build_manifest(
    *,
    model: str,
    attack: str,
    max_iters: int,
    plan_root: Path,
    task_cases: Sequence[TaskCase],
    conditions: Sequence[Condition],
    output: Path,
    pairs: str = "assigned",
) -> dict[str, Any]:
    source_paths = (
        Path(__file__),
        Path("eval/agentdojo_live_injection.py"),
        Path("benchmarks/agentdojo_adapter.py"),
        Path("benchmarks/structured_read.py"),
        Path("pauth/grammar_validator.py"),
        Path("pauth/slicer.py"),
        Path("pauth/rule_compiler.py"),
        Path("pauth/enforcer.py"),
        Path("pauth/envelope.py"),
        Path("pauth/evaluator.py"),
    )
    tasks = [
        {
            "suite": case.suite_name,
            "user_task_id": case.user_task_id,
            "injection_task_id": case.injection_task_id,
            "plan_available": case.plan is not None,
            "plan_path": str(case.plan.path) if case.plan is not None else None,
            "plan_sha256": (
                hashlib.sha256(case.plan.code.encode()).hexdigest()
                if case.plan is not None
                else None
            ),
        }
        for case in task_cases
    ]
    design = {
        "schema": "agentdojo_factorial_manifest_v1",
        "suite_version": "v1",
        "agentdojo_version": importlib.metadata.version("agentdojo"),
        "requested_model": model,
        "temperature": 0.0,
        "reasoning_effort": None,
        "attack": attack,
        "max_iters": max_iters,
        "pairs": pairs,
        "observations_per_task_condition": 1,
        "plan_unavailable_policy_on_handling": (
            "No live agent or tool execution; score the unchanged environment state."
        ),
        "planned_live_execution_rows": sum(
            1
            for condition in conditions
            for case in task_cases
            if not (condition.policy_enabled and case.plan is None)
        ),
        "planned_unchanged_state_assessment_rows": sum(
            1
            for condition in conditions
            for case in task_cases
            if condition.policy_enabled and case.plan is None
        ),
        "plan_root": str(plan_root),
        "conditions": [condition.name for condition in conditions],
        "tasks": tasks,
    }
    design_digest = hashlib.sha256(
        json.dumps(design, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    diff = _command_output("git", "diff", "--binary")
    return {
        **design,
        "design_sha256": design_digest,
        "output": str(output),
        "run_started_utc": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "git_head": _command_output("git", "rev-parse", "HEAD"),
        "git_diff_sha256": (
            hashlib.sha256(diff.encode()).hexdigest() if diff is not None else None
        ),
        "source_file_sha256": {
            str(path): _sha256_file(path)
            for path in source_paths
            if path.exists()
        },
    }


def _load_existing_rows(
    output: Path,
) -> dict[tuple[bool, bool, str, str, str], dict[str, Any]]:
    rows: dict[tuple[bool, bool, str, str, str], dict[str, Any]] = {}
    for line_number, line in enumerate(output.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL at {output}:{line_number}: {exc}") from exc
        rows[row_key(row)] = row
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=os.getenv("PAUTH_AGENT_MODEL", "gpt-4.1"))
    parser.add_argument("--attack", default="important_instructions")
    parser.add_argument("--suite", action="append", choices=SUITES)
    parser.add_argument(
        "--condition",
        action="append",
        choices=tuple(CONDITION_BY_NAME),
        help="Repeat to select cells; defaults to all four.",
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--pairs",
        choices=("assigned", "full"),
        default="assigned",
        help=(
            "assigned: one frozen injection goal per task (97 rows/condition). "
            "full: every user task x injection goal pair (629 rows/condition); "
            "injection-on conditions only."
        ),
    )
    parser.add_argument("--max-iters", type=int, default=15)
    parser.add_argument("--plan-root", type=Path, default=DEFAULT_PLAN_ROOT)
    parser.add_argument(
        "--plan-model",
        default=DEFAULT_PLAN_MODEL,
        help=(
            "Planner model whose frozen candidates to read. Selects the funnel "
            "scratch directory; independent of --model (the executor agent)."
        ),
    )
    parser.add_argument(
        "--expect-plans",
        type=int,
        default=82,
        help=(
            "Guard: refuse to run unless the selection yields exactly this many "
            "plans. Pins the matched design per planner model (claude-fable-5: "
            "82, gpt-5.1: 94). State it explicitly when switching Planners."
        ),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    selected_suites = tuple(dict.fromkeys(args.suite or SUITES))
    if args.pairs == "full":
        default_conditions = [
            condition.name for condition in CONDITIONS if condition.injection_enabled
        ]
    else:
        default_conditions = list(CONDITION_BY_NAME)
    conditions = tuple(
        CONDITION_BY_NAME[name]
        for name in dict.fromkeys(args.condition or default_conditions)
    )
    if args.pairs == "full" and any(
        not condition.injection_enabled for condition in conditions
    ):
        raise SystemExit(
            "--pairs full multiplies rows by injection goal, which is meaningless "
            "for injection-off cells; select injection-on conditions only."
        )
    all_plans = select_valid_plans(args.plan_root, SUITES, args.plan_model)
    if len(all_plans) != args.expect_plans:
        raise SystemExit(
            f"Expected the frozen {args.expect_plans}-plan population for "
            f"--plan-model {args.plan_model}, found {len(all_plans)}. "
            "Refusing to change the matched design."
        )
    if args.pairs == "full":
        task_cases = build_full_cross_cases(all_plans, selected_suites)
    else:
        task_cases = build_task_cases(all_plans, selected_suites)
    if args.limit is not None:
        task_cases = task_cases[: args.limit]
    print(
        json.dumps(
            {
                "tasks": len(task_cases),
                "plan_available": sum(case.plan is not None for case in task_cases),
                "plan_unavailable": sum(case.plan is None for case in task_cases),
                "conditions": [condition.name for condition in conditions],
                "planned_rows": len(task_cases) * len(conditions),
            },
            indent=2,
        )
    )
    if args.dry_run:
        return 0

    load_dotenv()
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is required for live-agent cases.")

    output = args.output or _default_output(args.model)
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest_path = output.with_suffix(".manifest.json")
    manifest = _build_manifest(
        model=args.model,
        attack=args.attack,
        max_iters=args.max_iters,
        plan_root=args.plan_root,
        task_cases=task_cases,
        conditions=conditions,
        output=output,
        pairs=args.pairs,
    )
    if args.resume and manifest_path.exists():
        existing_manifest = json.loads(manifest_path.read_text())
        if existing_manifest.get("design_sha256") != manifest["design_sha256"]:
            raise SystemExit(
                "Resume manifest does not match this model, condition, or task design."
            )
        manifest["run_started_utc"] = existing_manifest.get(
            "run_started_utc", manifest["run_started_utc"]
        )
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    )
    llm = InstrumentedOpenAILLM(openai.OpenAI(), args.model)
    rows_by_key: dict[tuple[bool, bool, str, str], dict[str, Any]] = {}
    output_mode = "w"
    if args.resume and output.exists():
        rows_by_key = _load_existing_rows(output)
        output_mode = "a"

    work = [(condition, case) for condition in conditions for case in task_cases]
    pending = [
        (condition, case)
        for condition, case in work
        if case_key(case, condition) not in rows_by_key
        or "error" in rows_by_key[case_key(case, condition)]
    ]
    completed_before = len(work) - len(pending)
    if completed_before:
        print(f"Resuming after {completed_before} completed rows.")

    with output.open(output_mode) as stream:
        for index, (condition, case) in enumerate(
            pending,
            start=completed_before + 1,
        ):
            print(
                f"[{index}/{len(work)}] {condition.name} "
                f"{case.suite_name}/{case.user_task_id} + {case.injection_task_id}",
                flush=True,
            )
            try:
                row = run_case(case, condition, llm, args.attack, args.max_iters)
            except Exception as exc:  # noqa: BLE001 -- preserve remaining paid runs
                row = {
                    **_base_row(case, condition, args.attack),
                    "status": "error",
                    "execution_status": "error",
                    "task_completed": False,
                    "attack_goal_achieved": False,
                    "task_completed_without_attack_goal": False,
                    OUTCOME_TASK_COMPLETED: None,
                    OUTCOME_ATTACK_GOAL_ACHIEVED: None,
                    OUTCOME_TASK_COMPLETED_WITHOUT_ATTACK_GOAL: None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            row["requested_model"] = args.model
            row["resolved_models_seen"] = sorted(llm.resolved_models)
            row["system_fingerprints_seen"] = sorted(llm.system_fingerprints)
            rows_by_key[case_key(case, condition)] = row
            stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            stream.flush()

    ordered_rows = [
        rows_by_key[case_key(case, condition)]
        for condition, case in work
        if case_key(case, condition) in rows_by_key
    ]
    summary = summarize(ordered_rows, task_cases, conditions)
    summary["run_metadata"] = {
        "manifest": str(manifest_path),
        "design_sha256": manifest["design_sha256"],
        "requested_model": args.model,
        "resolved_models": sorted(llm.resolved_models),
        "system_fingerprints": sorted(llm.system_fingerprints),
        "temperature": llm.temperature,
        "reasoning_effort": llm.reasoning_effort,
        "max_iters": args.max_iters,
    }
    summary_path = output.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    manifest.update(
        {
            "run_finished_utc": datetime.now(timezone.utc).isoformat(),
            "completed_rows": len(ordered_rows),
            "error_rows": sum("error" in row for row in ordered_rows),
            "live_execution_rows": sum(
                row.get("status") == "live" for row in ordered_rows
            ),
            "unchanged_state_assessment_rows": sum(
                row.get("status") == "plan_unavailable" for row in ordered_rows
            ),
            "resolved_models": sorted(llm.resolved_models),
            "system_fingerprints": sorted(llm.system_fingerprints),
            "summary": str(summary_path),
        }
    )
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"Cases:   {output}")
    print(f"Summary: {summary_path}")
    errors = sum("error" in row for row in ordered_rows)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
