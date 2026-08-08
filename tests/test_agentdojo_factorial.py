import openai
from agentdojo.functions_runtime import FunctionCall, FunctionsRuntime
from agentdojo.task_suite.load_suites import get_suites
from agentdojo.types import ChatAssistantMessage

from benchmarks.agentdojo_adapter import load_suite
from eval.agentdojo_factorial import (
    CONDITIONS,
    Condition,
    InstrumentedToolsExecutor,
    TaskCase,
    build_full_cross_cases,
    build_task_cases,
    case_key,
    row_key,
    run_case,
    summarize,
)
from eval.agentdojo_live_injection import (
    InstrumentedOpenAILLM,
    build_evaluation_cases,
    select_valid_plans,
)
from eval.metrics import (
    OUTCOME_ATTACK_GOAL_ACHIEVED,
    OUTCOME_TASK_COMPLETED,
    OUTCOME_TASK_COMPLETED_WITHOUT_ATTACK_GOAL,
)
from pauth import prepare
from pauth.enforcer import Enforcer
from pauth.envelope import EnvelopeStore, KeyRing


def _assistant_call(call: FunctionCall) -> ChatAssistantMessage:
    return ChatAssistantMessage(role="assistant", content=None, tool_calls=[call])


def test_task_cases_cover_97_and_freeze_the_existing_82_pairs():
    plans = select_valid_plans()
    cases = build_task_cases(plans)
    assert len(cases) == 97
    assert sum(case.plan is not None for case in cases) == 82
    assert len({case.task_key for case in cases}) == 97

    old_pairs = {
        (case.plan.suite_name, case.plan.task_id): case.injection_task_id
        for case in build_evaluation_cases(plans)
    }
    new_pairs = {
        case.task_key: case.injection_task_id
        for case in cases
        if case.plan is not None
    }
    assert new_pairs == old_pairs
    assert build_task_cases(plans) == cases


def test_row_key_is_condition_then_task_key():
    case = TaskCase("banking", "user_task_0", "injection_task_0", None)
    condition = Condition(True, False)
    row = {
        "policy_enabled": True,
        "injection_enabled": False,
        "suite": "banking",
        "user_task_id": "user_task_0",
        "injection_task_id": "injection_task_0",
    }
    assert row_key(row) == case_key(case, condition)


def test_full_cross_cases_cover_all_629_pairs():
    plans = select_valid_plans()
    cases = build_full_cross_cases(plans)
    assert len(cases) == 629
    assert len({(case.task_key, case.injection_task_id) for case in cases}) == 629
    by_task_plan = {case.task_key: case.plan is not None for case in cases}
    assert sum(by_task_plan.values()) == 82
    assigned = build_task_cases(plans)
    assigned_pairs = {(case.task_key, case.injection_task_id) for case in assigned}
    full_pairs = {(case.task_key, case.injection_task_id) for case in cases}
    assert assigned_pairs <= full_pairs


def test_common_executor_is_permissive_only_when_policy_is_off():
    suite = get_suites("v1")["banking"]
    spec = load_suite("banking")
    prepared = prepare(
        "def run():\n    get_balance()\n",
        spec.tool_names(),
        spec.tool_signer(),
    )
    enforcer = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), spec.tool_signer())
    runtime = FunctionsRuntime(suite.tools)
    env = suite.load_and_inject_default_environment({})
    off_plan = FunctionCall(
        function="send_money",
        args={
            "recipient": "US133000000121212121212",
            "amount": 1,
            "subject": "factorial test",
            "date": "2022-01-01",
        },
        id="call-off",
    )

    policy_off = InstrumentedToolsExecutor(None, (), policy_enabled=False)
    _, _, _, messages, _ = policy_off.query(
        "",
        runtime,
        env.model_copy(deep=True),
        [_assistant_call(off_plan.model_copy(deep=True))],
    )
    assert messages[-1]["error"] is None
    assert policy_off.events[-1].executed
    assert policy_off.events[-1].policy_permitted is None

    policy_on = InstrumentedToolsExecutor(enforcer, (), policy_enabled=True)
    _, _, _, messages, _ = policy_on.query(
        "",
        runtime,
        env.model_copy(deep=True),
        [_assistant_call(off_plan.model_copy(deep=True))],
    )
    assert messages[-1]["error"].startswith("PAuthDenied:")
    assert not policy_on.events[-1].executed
    assert policy_on.events[-1].policy_permitted is False


def test_policy_on_missing_plan_is_scored_without_an_llm_request():
    case = TaskCase("banking", "user_task_11", "injection_task_0", None)
    llm = InstrumentedOpenAILLM(openai.OpenAI(api_key="unused"), "gpt-4.1")
    row = run_case(
        case,
        Condition(True, True),
        llm,
        "important_instructions",
        15,
    )
    assert llm.requests == 0
    assert row["status"] == "plan_unavailable"
    assert row["plan_available"] is False
    assert row["task_completed"] is False
    assert isinstance(row["attack_goal_achieved"], bool)
    assert row["task_completed_without_attack_goal"] is False


def test_summary_has_all_task_and_common_plan_populations():
    cases = [
        TaskCase("banking", "user_task_0", "injection_task_0", object()),
        TaskCase("banking", "user_task_1", "injection_task_1", None),
    ]
    condition = Condition(True, True)
    rows = []
    for case, task_completed in zip(cases, (True, False), strict=True):
        rows.append(
            {
                "policy_enabled": True,
                "injection_enabled": True,
                "suite": case.suite_name,
                "user_task_id": case.user_task_id,
                "plan_available": case.plan is not None,
                "status": "live" if case.plan is not None else "plan_unavailable",
                "execution_status": (
                    "live" if case.plan is not None else "plan_unavailable"
                ),
                OUTCOME_TASK_COMPLETED: task_completed,
                OUTCOME_ATTACK_GOAL_ACHIEVED: False,
                OUTCOME_TASK_COMPLETED_WITHOUT_ATTACK_GOAL: task_completed,
            }
        )
    summary = summarize(rows, cases, (condition,))
    cell = summary["conditions"][condition.name]
    assert cell["population_97"]["expected_denominator"] == 2
    assert cell["population_97"][OUTCOME_TASK_COMPLETED]["count"] == 1
    assert cell["common_82"]["expected_denominator"] == 1
    assert cell["common_82"][OUTCOME_TASK_COMPLETED]["rate"] == 1.0
