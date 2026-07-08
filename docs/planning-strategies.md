# Planning strategies

gateway は、ひとつの prompt-to-code 手法に製品を賭けてはならない。PAuth の安定
した境界は次のとおり:

```text
user intent -> restricted imperative run() code -> pauth.prepare() -> rules
```

`pauth.prepare()` より前のすべては、差し替え可能な A1 strategy である。

## Strategy Names

config、JSON メッセージ、環境変数では、これらの正準名を使うこと:

| Name | Status | Meaning |
|---|---|---|
| `deterministic` | implemented | 既知の prompt パターンに対する Regex recognizer。 |
| `llm-freeform` | implemented | grammar repair とオプションの judge を備えた汎用 LLM A1。 |
| `auto` | implemented | recognizer fast-path、不一致なら `llm-freeform` へフォールバック（既定の main ingress 戦略、S2。別名 `hybrid`）。 |
| `interactive-structuring` | registered | code 生成前の clarification ループ。 |
| `specialized-codegen` | registered | 専用の imperative-code モデル + validator リトライ。 |
| `formal-semantic` | registered | Formal NL parser / semantic analysis パス。 |

main ingress（`AgentChannel`）の既定は `auto`（`PAUTH_PLANNER_STRATEGY` 未設定時）。
`PAUTH_PLANNER_SUITE` が無ければ `auto` はフォールバック先を持たず `deterministic` と同じ
受理集合になる。`Gateway.submit_user_prompt` は `deterministic` を直接使う。

Runtime selection:

```bash
PAUTH_PLANNER_STRATEGY=llm-freeform
PAUTH_PLANNER_SUITE=shopping
PAUTH_PLANNER_MODEL=gpt-4.1
PAUTH_PLANNER_MAX_RETRIES=3
```

registered だが未実装の strategy は、明確なメッセージとともに意図的に reject する。
それらは strategy slot であって、黙った fallback ではない。

## Strategy 1: Interactive Structuring

A1 生成の前に、ガイド付きの「Grill me」スタイルの対話を使う。

Flow:

1. ユーザーの生の自然言語タスクから始める。
2. 欠けている operand、条件、ツール、日付、数量、recipient、および曖昧性解消に
   ついて、的を絞った質問をする。
3. 構造化されたタスク prompt または構造化された intent object を生成する。
4. その構造化表現を code generator に渡す。
5. 生成された imperative code を `pauth.prepare()` で検証する。

これは入力タスクが underspecified または曖昧なときに有用。また早期のプロダクト
作業にも適している。モデルが黙って値を捏造させるのではなく、欠けている意図を
可視化するからだ。

Hard constraint: この strategy は計画の前に user-interaction surface を要する。
gateway が user-facing な prompt ステップを所有していない限り、すでに走っている
エージェントに対する「network config only」と同じではない。

Failure mode:

- agent firewall を装った form-filling プロダクトになりうる。
- 質問が広すぎると、ユーザーは曖昧な答えを返し、planner はやはり詳細を捏造する。
- 結果の構造化 prompt が監査可能でなければ、この strategy は hallucination を
  code 生成から prompt 書き換えへ移すだけになる。

Design implication:

- interactive collector を PAuth core の外に保つ。
- その出力を、もうひとつの planner 入力として扱う。
- 生の prompt、質問、回答、最終的な構造化 prompt、生成された code、検証結果を
  保存する。

## Strategy 2: Specialized Imperative-Code Model

ユーザーのタスク + tool schema から restricted imperative `run` code を吐くこと
だけを仕事とするモデルを訓練ないしチューニングする。

Flow:

1. ユーザータスクと利用可能な tool schema を提供する。
2. 専用モデルが `def run(...): ...` を吐く。
3. 決定的な validator を走らせる: grammar、semantic checks、slicing、rule
   compilation。
4. 検証が失敗したら、正確な validator エラーをフィードバックしてリトライする。
5. リトライ予算が尽きたら、タスクを reject する。

これが機能すれば、gateway は手の込んだ prompt-template ロジックを必要としない。
複雑さはモデルの訓練/評価と、小さな決定的 repair ループへ移る。

この strategy が魅力的なのは、runtime architecture がシンプルなまま保たれる
からだ:

- 1回の generation call;
- 1つの validator;
- bounded retries;
- 失敗時の default-deny。

居心地の悪い部分はデータだ。十分な高品質の
`prompt + tool schema -> restricted run() code` の例がなければ、この strategy は
ラベルが良いだけの希望的観測にすぎない。モデルは grammar の妥当性だけでなく、
exact intent capture で評価されねばならない。

Failure mode:

- grammar 上は妥当だがユーザー意図を落とす code。
- よくあるベンチマークは満たすが、実際のユーザー prompt では失敗する code。
- toy suite に overfit し、実際の MCP ツールへ転移しない訓練データ。

Design implication:

- validator フィードバックを model-agnostic に保つ。
- 失敗したすべての generation と validator エラーを訓練データとしてログする。
- grammar 成功メトリクスを intent-faithfulness メトリクスから分離する。

## Strategy 3: Formal Natural-Language Analysis

自然言語 prompt を形式化し、syntactic/semantic analysis を通して翻訳することで、
LLM の役割をできる限り縮小する。

Candidate shape:

1. 受理するタスク言語を制限する。
2. formal grammar で prompt をパースする。例えば categorial grammar や関連する
   compositional semantic parser。
3. パース済みの semantic form を tool action、operand、guard、data dependency へ
   マップする。
4. restricted imperative `run` code を吐く。
5. `pauth.prepare()` で検証する。

これは最も知的にクリーンな方向だが、最も product-ready でない方向でもある。
受理するタスク言語を意図的に狭くするか、プロダクトが明示的な controlled language
を許容できる場合にのみ実用的になる。

Failure mode:

- 実際のユーザー prompt が grammar の外に落ちる。
- grammar が保守不能な例外の山へ成長する。
- 手選びの例ではカバレッジが良く見え、production の言語では崩壊する。
- 曖昧性解消が、いつの間にかもうひとつの隠れた LLM 的コンポーネントになる。

Design implication:

- デフォルトパスではなく research slot として保つ。
- template と formal semantics が現実的な、狭くて high-value なドメインに使う。
- rejection rate を correctness とは別に測る。多くの prompt を reject する formal
  parser でも、受理した prompt が信頼できるなら依然として価値がある。

## Current Implementation Mapping

Current concrete planners:

- `DeterministicRecognizerPlanner`: 既知の prompt パターンに対する厳格な baseline。
- `LLMFreeformPlanner`: grammar repair とオプションの judge を備えた汎用モデル。
- `AutoPlanner`: recognizer fast-path、その後 `LLMFreeformPlanner` へフォールバック（S2）。

Planned strategy slots:

- `InteractiveStructuringPlanner`: user-facing な clarification セッションをラップ
  し、その後別の code generator へ委譲する。
- `SpecializedCodegenPlanner`: 専用の imperative-code モデルを呼び、リトライは
  主に validator フィードバックに頼る。
- `FormalSemanticPlanner`: controlled natural language を semantic form へパース
  し、restricted imperative code を吐く。

どちらの planned strategy も、依然として restricted imperative code を返し、
`pauth.prepare()` を通過せねばならない。どの strategy も rule を直接吐くことは
許されない。
