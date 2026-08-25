# 評価指標の四軸 — 理由記入用の下書き

目的: 利用者のエージェント利用体験を大きく変えずに、タスクごとの最小権限付与を
自動化して安全性を上げる。この目的から、測るべき指標を四軸(可用性・安全性・
コスト・時間)に区分する。

注: 本ファイルは下書き。確定したら `docs/SYSTEM_MODEL.md` 第 4 部へ反映する。
「理由」欄は空欄のまま。

## 可用性

| 指標(日本語名 / 英語名) | 性能を測る対象 | 理由 |
|---|---|---|
| 表現可能性 / `FEASIBILITY_EXPRESSIBLE` | GrammarValidator — DSL の広さの上限 |  |
| policy 生成成功 / `SYNTHESIS_POLICY_COMPILED` | Planner — DSL に収まるコードを書けるか |  |
| 実行時クラッシュなし / `RELIABILITY_RUNTIME_CRASH_FREE` | Planner — 生成コードの実行健全性 |  |
| 計画トレース許可 / `CONFORMANCE_PLAN_TRACE_PERMITTED` | Slicer+RuleCompiler — 計画を狭め過ぎていないか |  |
| ツール呼び出しの不足なし / `GT_NO_MISSING_CALLS` | パイプライン全体 — 不足なし |  |
| タスク結果 / `OUTCOME_TASK_COMPLETED` | 系全体 — 目標状態への到達 |  |

## 安全性

| 指標(日本語名 / 英語名) | 性能を測る対象 | 理由 |
|---|---|---|
| ツール呼び出しの過剰なし / `GT_NO_EXCESS_CALLS` | パイプライン全体 — 過剰なし |  |
| インジェクション拒否 / `AUX_INJECTIONS_DENIED` | Enforcer — 固定攻撃の拒否 |  |
| 攻撃ゴール達成 / `OUTCOME_ATTACK_GOAL_ACHIEVED` | 系全体 — 割り当てた攻撃ゴールへの到達(低いほどよい) |  |

## 可用性と安全性にまたがる結合指標

四軸のどちらか一方には割り当てられない。両軸の連言としてのみ意味を持つ。

| 指標(日本語名 / 英語名) | 性能を測る対象 | 理由 |
|---|---|---|
| 過不足なし / `GT_EXACT_AUTHORIZATION` | パイプライン全体 — 不足なし AND 過剰なし |  |
| 攻撃ゴール未達でのタスク完了 / `OUTCOME_TASK_COMPLETED_WITHOUT_ATTACK_GOAL` | 系全体 — タスク結果 AND 攻撃ゴール未達 |  |

## コスト

| 指標(日本語名 / 英語名) | 性能を測る対象 | 状態 | 理由 |
|---|---|---|---|
| ツール呼び出し費用 / `COST_TOOL_CALLS` | 系全体 — 一計画あたりの許可呼び出し数の平均 | 第 4 部で定義済み |  |
| (未命名)トークン費用 | Planner — 計画生成の入出力トークン数 | 実装のみ(`eval/fpfn.py` の `prompt_tokens` / `completion_tokens`) |  |
| (未命名)金額費用 | Planner — 計画生成の課金額 | 実装のみ(`eval/fpfn.py` の `cost_usd`) |  |
| (未命名)追加費用 / `ADDITIONAL_COST` | 系全体 — 素のエージェントに対する増分 | 名称のみ(`eval/__init__.py`)、定義なし |  |

## 時間

| 指標(日本語名 / 英語名) | 性能を測る対象 | 状態 | 理由 |
|---|---|---|---|
| (未命名)ゲートウェイ処理時間 / `LATENCY` | 外殻 — 入口から出口までの処理時間の総和 | 実装のみ(`eval/grill_eval.py`)、第 4 部に定義なし |  |
| (未命名)タスク所要時間 | 系全体 — 一事例あたりの実行秒数 | 実装のみ(`eval/agentdojo_factorial.py` の `elapsed_seconds`) |  |
| (未命名)計画時の追加待ち時間 | Planner — 計画時に利用者が待つ増分 | 未実装 |  |
