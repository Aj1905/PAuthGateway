"""論文第 5 節の測定本体。

1 タスクにつき次を行う(論文 図 6 の A1→A3、B1→B4 に対応):

1. **Planner** -- run コード(計画)を得る。キャッシュ済みの計画があれば
   それを使い、無ければ API キーがある場合のみ生成する。
2. **GrammarValidator / Slicer / RuleCompiler** -- DSL 検査、スライス導出、
   ルール compile(すべて決定的)。
3. **良性実行** -- Enforcer 越しに run コードを実行する。拒否された
   ツール呼び出しが 1 つでもあればそのタスクは偽陽性(FP)。
4. **強制注入** -- 各注入ツール呼び出しを Enforcer に提示する。許可されたら
   偽陰性(FN)。

このモジュールは測定しかしない。0 という結果を前提にしない。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from pauth import (
    DSLRejectionError,
    Enforcer,
    EnvelopeStore,
    KeyRing,
    check_injection,
    execute_generated_code,
    prepare,
)
from pauth.codegen import generate_code, has_api_key
from pauth.grammar_validator import DSL_PROFILE_EXTENDED, DSL_PROFILE_PAPER
from pauth.suites.base import SuiteSpec, TaskSpec

from repro.rules import RuleCounts, count_rules

REPO_ROOT = Path(__file__).resolve().parents[3]
PLAN_CACHE = REPO_ROOT / "tests" / "experiment" / "cache"

# 論文 表 1 のスイート。banking/slack/workspace/travel は AgentDojo、
# shopping は論文が追加したスイートで、本リポジトリの実装を使う。
PAPER_SUITES: tuple[str, ...] = ("banking", "slack", "workspace", "travel", "shopping")
AGENTDOJO_SUITES: tuple[str, ...] = ("banking", "slack", "workspace", "travel")

DSL_PROFILES = (DSL_PROFILE_PAPER, DSL_PROFILE_EXTENDED)  # "g1", "g2"


class SuiteUnavailable(RuntimeError):
    """スイートを読み込めない(依存パッケージ欠落など)。"""


def load_suite(name: str) -> SuiteSpec:
    """論文のスイート名から :class:`SuiteSpec` を得る。"""
    if name == "shopping":
        from pauth.suites.shopping import build_suite

        return build_suite()
    if name in AGENTDOJO_SUITES:
        try:
            from benchmarks.agentdojo_adapter import load_suite as load_agentdojo
        except Exception as exc:  # noqa: BLE001 -- 依存が無い場合を含む
            raise SuiteUnavailable(f"AgentDojo を読み込めない: {exc}") from exc
        return load_agentdojo(name)
    raise SuiteUnavailable(f"未知のスイート: {name}")


@dataclasses.dataclass
class TaskOutcome:
    """1 タスク分の測定結果。"""

    suite: str
    task_id: str

    # 計画(Planner)の入手可否
    plan_status: str            # "ok" | "unavailable" | "dsl-rejected"
    plan_detail: str
    plan_cached: bool
    planner_model: str
    prompt_tokens: int
    completion_tokens: int

    # ルール(図 9)。``n_rules`` は compile されたルール数 = 計画中の
    # ツール呼び出し地点の数。``rule_counts`` はその内訳(オペランド/assert)。
    n_rules: int
    rule_counts: RuleCounts | None

    # 良性実行(表 2 の FP 側)
    benign_calls: int
    benign_denied: list[str]
    crashed: str | None
    tool_errors: list[str]

    # 強制注入(表 2 の FN 側)
    n_injections: int
    permitted_injections: list[str]

    @property
    def checkable(self) -> bool:
        """計画が得られて表 2 の判定対象になったか。"""
        return self.plan_status == "ok"

    @property
    def is_false_positive(self) -> bool:
        return self.checkable and bool(self.benign_denied)

    @property
    def n_false_negatives(self) -> int:
        return len(self.permitted_injections)

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["rule_counts"] = self.rule_counts.to_dict() if self.rule_counts else None
        data["is_false_positive"] = self.is_false_positive
        data["n_false_negatives"] = self.n_false_negatives
        return data


def _plan_cache_path(suite_name: str, task: TaskSpec) -> Path:
    short_id = task.id.split(".")[-1]
    return PLAN_CACHE / suite_name / f"{short_id}.py"


def _unavailable(
    suite: SuiteSpec, task: TaskSpec, status: str, detail: str, *,
    model: str = "", cached: bool = False,
    prompt_tokens: int = 0, completion_tokens: int = 0,
) -> TaskOutcome:
    return TaskOutcome(
        suite=suite.name,
        task_id=task.id,
        plan_status=status,
        plan_detail=detail,
        plan_cached=cached,
        planner_model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        n_rules=0,
        rule_counts=None,
        benign_calls=0,
        benign_denied=[],
        crashed=None,
        tool_errors=[],
        n_injections=len(task.forced_injections),
        permitted_injections=[],
    )


def run_task(
    suite: SuiteSpec,
    task: TaskSpec,
    *,
    model: str,
    dsl_profile: str,
    allow_api: bool,
    use_cache: bool = True,
) -> TaskOutcome:
    """1 タスクを Planner→RuleCompiler→Enforcer まで通す。"""
    prompt_tokens = completion_tokens = 0
    cached = False
    planner_model = model

    # ---- Planner: run コード(計画) -------------------------------------
    if task.reference_code is not None:
        code = task.reference_code
        plan_detail = "スイート同梱の参照計画"
        planner_model = "(none)"
    else:
        cache_path = _plan_cache_path(suite.name, task) if use_cache else None
        if (cache_path is None or not cache_path.exists()) and not (
            allow_api and has_api_key()
        ):
            return _unavailable(
                suite, task, "unavailable",
                "キャッシュ済みの計画が無く、API 生成も許可されていない",
            )
        try:
            result = generate_code(task.prompt, suite.tool_docs(), model, cache_path)
        except Exception as exc:  # noqa: BLE001 -- API/ネットワーク側の失敗
            return _unavailable(
                suite, task, "unavailable",
                f"Planner の失敗: {type(exc).__name__}: {exc}",
            )
        code = result.code
        prompt_tokens = result.prompt_tokens
        completion_tokens = result.completion_tokens
        cached = result.cached
        planner_model = result.model
        plan_detail = "キャッシュ済みの計画" if cached else f"{result.model} が生成"

    # ---- GrammarValidator / Slicer / RuleCompiler ------------------------
    try:
        prepared = prepare(
            code, suite.tool_names(), suite.tool_signer(), dsl_profile=dsl_profile
        )
    except DSLRejectionError as exc:
        return _unavailable(
            suite, task, "dsl-rejected", f"DSL({dsl_profile})が棄却: {exc}",
            model=planner_model, cached=cached,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        )

    # ---- 良性実行(B1-B4) ------------------------------------------------
    env = suite.make_env()
    tool_executor = suite.tool_executor_factory(env)
    store = EnvelopeStore(KeyRing())
    enforcer = Enforcer(prepared.rules, store, suite.tool_signer())
    report = execute_generated_code(
        prepared.source, enforcer, suite.tool_params(), tool_executor
    )
    denied = [
        f"{e.tool}({', '.join(map(repr, e.args))}) :: {e.decision.reason}"
        for e in report.denied
    ]

    # ---- 強制注入 --------------------------------------------------------
    permitted: list[str] = []
    for injection in task.forced_injections:
        decision = check_injection(enforcer, injection.tool, injection.args)
        if decision.permit:
            permitted.append(f"{injection} :: {decision.reason}")

    return TaskOutcome(
        suite=suite.name,
        task_id=task.id,
        plan_status="ok",
        plan_detail=plan_detail,
        plan_cached=cached,
        planner_model=planner_model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        n_rules=len(prepared.rules),
        rule_counts=count_rules(prepared.rules),
        benign_calls=len(report.events),
        benign_denied=denied,
        crashed=report.crashed,
        tool_errors=report.tool_errors,
        n_injections=len(task.forced_injections),
        permitted_injections=permitted,
    )


def run_suites(
    names: tuple[str, ...] = PAPER_SUITES,
    *,
    model: str = "gpt-4.1",
    dsl_profile: str = DSL_PROFILE_EXTENDED,
    allow_api: bool = False,
    limit: int | None = None,
    progress: bool = True,
) -> tuple[list[TaskOutcome], dict[str, str]]:
    """複数スイートを回す。戻り値は(結果一覧, 読み込めなかったスイート)。"""
    outcomes: list[TaskOutcome] = []
    skipped: dict[str, str] = {}
    for name in names:
        try:
            suite = load_suite(name)
        except SuiteUnavailable as exc:
            skipped[name] = str(exc)
            if progress:
                print(f"[{name}] スキップ: {exc}")
            continue
        tasks = suite.tasks[:limit] if limit else suite.tasks
        for i, task in enumerate(tasks, 1):
            outcome = run_task(
                suite, task, model=model, dsl_profile=dsl_profile,
                allow_api=allow_api,
            )
            if progress:
                mark = (
                    "ok" if outcome.checkable and not outcome.is_false_positive
                    and not outcome.n_false_negatives
                    else outcome.plan_status
                )
                if outcome.checkable:
                    flags = []
                    if outcome.is_false_positive:
                        flags.append(f"FP={len(outcome.benign_denied)}")
                    if outcome.n_false_negatives:
                        flags.append(f"FN={outcome.n_false_negatives}")
                    if flags:
                        mark = " ".join(flags)
                print(f"[{name}] ({i}/{len(tasks)}) {task.id}: {mark}")
            outcomes.append(outcome)
    return outcomes, skipped
