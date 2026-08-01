# 用語集

`pauth/`、`gateway/`、`eval/`、および設計文書の全体で用いる語彙の正確な定義。
用語はコードに根ざしている。区別が微妙な箇所では、正確な境界を明記する。

**本書の構成.** 第 1 部は、PAuth の中核パイプラインを構成する**ノード**を
処理順に定義し、各ノードの直下に、そのノードで初めて登場する概念をぶら下げ
る。一つの概念は一箇所でだけ定義し、他所からは参照する。ノードの軸に載らな
い横断的な語彙(評価の分類体系、トレース比較、ベンチマーク由来の語、測定上
の知見)は第 2 部に置く。

---

# 第 1 部 — パイプラインのノード

## 0. 系全体

- **PAuth パイプライン.** 自然言語プロンプトから実行時の認可判定までの中核
  連鎖。本書のノード軸はこの連鎖の処理順に従う。

  ```text
  prompt ─► [1 Planner] ─► run() コード
         ─► [2 GrammarValidator] ─► 検証済みコード
         ─► [3 Slicer] ─► slices
         ─► [4 Rule compiler] ─► rules
  実行時: エージェントの各ツール呼び出し
         ─► [5 Enforcer] ⇄ [6 EnvelopeStore]
         ─► [7 ツール供給源 (runner)] ─► 実ツール実行
  ```

  Planner(ノード 1)だけが非決定的(LLM)であり、ノード 2 以降はすべて
  決定的である。実装の対応: `pauth/codegen.py`(A1 プロンプト)、
  `pauth/grammar.py`、`pauth/slicing.py`(A2)、`pauth/rules.py`(A3)、
  `pauth/enforcer.py`(B1–B4)、`pauth/envelope.py`、`pauth/suites/base.py`。

- **計画は一度だけ / plan-once.** `Gateway.submit_user_prompt` はセッション
  ごとに一度しか呼べず、以後のすべてのツール呼び出しは
  `Gateway.handle_tool_call` で執行だけを受ける。ゲートウェイがエージェントの
  入力に基づいて計画を作り直すことはない(`ARCHITECTURE.md` §4)。

- **A1–A4 / B1–B4.** 論文(2603.17170)Figure 6 の**矢印ラベル**であり、
  独立した段階の定義ではない。A 系列 = task submission pipeline(計画時、
  タスクにつき一度): A1 コード生成、A2 スライス導出、A3 ルールコンパイル、
  A4 タスク文をエージェントへ渡す。B 系列 = task execution pipeline
  (実行時、ツール呼び出しごと): B1–B2 呼び出しの横取りとルール・envelope
  照合、B3 許可/拒否の判定、B4 結果の envelope 記録。個別番号は論文の図を
  引用するときにだけ使い、本リポジトリの文書では空間ノード名
  (Planner / Slicer / Rule compiler / Enforcer / EnvelopeStore)と
  「計画時/実行時」の二相で書くこと。注意: 一部の文書は B1 を
  「デフォルト拒否」の意味で単独使用しているが(`THREAT_MODEL.md` §2)、
  これは論文のラベルとは一致しない読み替えである。

## 1. Planner / 計画器(A1)

唯一の LLM 段階。プロンプト+ツールスキーマを入力に、制限文法(ノード 2)に
従う `run()` 関数を出力する。唯一の非決定的段階であり、表現可能性の次に来る
品質のボトルネック。`--planner oneshot|agentic` フラグで選択する。境界の
実装は `gateway/planning/planner.py`、LLM 版は
`gateway/planning/agentic_planner.py`。

このノードで登場する概念:

- **oneshot / agentic.** Planner の二戦略。oneshot は一回生成で終わり、
  agentic は文法棄却理由と実行時フィードバックを LLM に返して再生成する
  ループを持つ。
- **exec-repair / 実行時修復.** Agentic-Planner の一段階: 文法的に妥当な候補を
  模擬環境で試走させ、クラッシュ(ノード 7)をフィードバックして修復する。
  再試行後もクラッシュするなら、拒否番兵に置き換える。クラッシュは根絶する
  が、タスク成功は上がらない(`pass` の plan は何もしない)。
- **Reject sentinel / 拒否番兵.** `def run(): pass` — agentic なパイプラインが
  最後に頼る、正直な「何もしない」plan。黙って誤る plan より安全である
  (例えば `amount=None` で送金する代わりに、拒否する)。
- **Intent judge / 意図判定器.** agentic な修復の間に、コードがプロンプトの
  意図を(過剰・欠落の観点で)捉えているかを検査する、別個の LLM。雑音が
  多く(見解に基づく判定)、正解データである `utility()`/`ground_truth()`
  (第 2 部)の方が望ましい。
- **Prose-locked value / 散文に埋もれた値.** 非構造化テキストの返り値の中に
  しか存在しない値(`.txt` の中の請求金額)であり、文字列演算を持たない文法
  では抽出できない。表現可能性(`FEASIBILITY_EXPRESSIBLE`、第 2 部)の失敗の
  主因。

## 2. GrammarValidator / 制限文法検証器

Planner 境界の契約の執行点。Planner の出力コードを構文検査
(`parse_and_validate`)→ 死コード除去(`strip_dead_code`)→ 意味検査
(`validate_semantics`)にかける。実装は `pauth/grammar.py` の一箇所だが、
呼び出し側は Planner 側のリトライループと `pauth.prepare()` 内の再検証の
二つある(`ARCHITECTURE.md` §1.1)。

このノードで登場する概念:

- **Restricted grammar / 制限文法.** Planner が出力しなければならない狭い
  Python 部分集合(メソッド呼び出しなし、文字列演算なし、`while` なし、
  有界の `for`、flat/nested-if ≤3)。スライシングとルールコンパイルを解析
  可能に保つために狭くしてあり、同じ制限が `FEASIBILITY_EXPRESSIBLE` の上限
  を定める(論文付録 A)。
- **Grammar rejection / 文法棄却.** 検証器がコードを拒絶すること。Enforcer の
  拒否(ノード 5)とは別の失敗クラス(Planner 失敗)として評価ファネルに
  計上される。agentic 経路では棄却理由が LLM に返され、再生成の入力になる。

## 3. Slicer / スライサ(A2)

決定的。検証済みの `run()` から、ツール呼び出しごとの**スライス**を導出する。
実装は `pauth/slicing.py`。

このノードで登場する概念:

- **Slice / スライス.** ツール呼び出しごとの記号的な仕様: 各オペランドの式
  +その call に到達するための経路条件(guard)。
- **Guard / ガード.** call に対する経路条件(`if C:` → guard `C`。入れ子 →
  `C1 and C2`。else → `not C`)。Enforcer は、すべての guard の成立を要求
  する。
- **Control operand / 制御オペランド.** 副作用のある call の効果を決定する
  オペランド(送金先、金額、宛先など)。スライスが式として拘束する対象で
  あり、第 2 部の参照照合(ツール名+制御オペランド)でも比較の単位になる。

## 4. Rule compiler / ルールコンパイラ(A3)

決定的。スライスを**ルール**にコンパイルする(論文 Algorithm 1)。実装は
`pauth/rules.py`。ここまで(ノード 1〜4)がタスク開始時に一度だけ走り、
以降は実行時執行(ノード 5〜7)に移る。

このノードで登場する概念:

- **Rule / ルール.** Enforcer が具体的な call を照合する対象となる、コンパイル
  済みのスライス。いずれかの rule に一致すればよい意味論(any-match)。
- **コンパイル済み policy.** あるタスクについてコンパイルされたルールの全体。
  履歴依存の認可関係 `P(h, call)` を定める(第 2 部「観測モデル」)。一回の
  具体的な実行が観測するのはその一断面にすぎない。

## 5. Enforcer / 執行器(B1–B4)

実行時の執行点。各ツール呼び出しを横取りし、ルールに照らして検査し
(rule が存在し、guard が成立し、すべてのオペランドがスライスされた値に
等しい)、許可されれば実行させ、結果を署名付き envelope(ノード 6)に包む。
実装は `pauth/enforcer.py`。

このノードで登場する概念:

- **Default-deny / デフォルト拒否.** 完全一致するルールを持たないすべての
  呼び出しを拒絶する既定の意味論(論文 5.2 節)。拒絶理由は監査可能性の
  ため、呼び出し元へそのまま提示される。
- **Denial / 拒否(`_Denied`).** 一致する rule がなかったため、Enforcer が
  call を遮断した。`_Denied` として送出され、別に捕捉され、クラッシュ
  (ノード 7)としては数えない。拒否の正誤は、その具体的な call が認可される
  べきだったかどうかに対してのみ定まる。
- **Two authorization paths / 二つの認可経路.** 横取りされたすべてのツール
  呼び出しは、その制御オペランドの出所が機械的に*証明可能*かどうかで分岐し、
  二つの経路のちょうど一方を取る。
  - **自動認可経路 (auto-authorization path).** オペランドの出所が検証可能
    (プロンプト直書き、tool フィールド、計算値、構造化)であるため、
    Enforcer が再導出し、人間なしで許可/拒否を判定する。ヘッドレス評価が
    完了できる唯一の経路である。その判定は、plan-トレース整合、参照忠実性、
    固定攻撃プローブで評価される。どれか一つが普遍的な安全性の証明になる
    わけではない。
  - **人間確認経路 (human-confirmation path).** オペランドが信頼できない
    由来(例: 散文から LLM が抽出した値、`verifiable=False`)であるため
    証明が*できず*、call は `PendingConfirmation` として保留され、人間に
    回される。人間の承認/却下がそのまま認可であるため、誤った確認は過剰
    許可に、保守的な確認は過少許可になりうる。ヘッドレス実行(`Confirmer`
    なし)では、これらの call は保留のまま → 未完了となる。人間を介した
    配備(`InteractiveConfirmer`)では完了できる。
- **Confirmation gate / 確認ゲート.** 信頼できない由来の値が、副作用のある
  call の制御オペランドに達したとき、人間の確認のために call を保留する
  機構。人間確認経路の実装点。コード: `_confirmation_gate` →
  `PendingConfirmation` → `Confirmer`。
- **Free operand / 自由オペランド.** 配備者が `PolicyAwareEnforcer`
  (`gateway/runtime/policy.py`)で `(tool, parameter)` の組に印を付け、
  オペランド検査を飛ばす対象。検索クエリや自由記述のメッセージ本文など、
  取引上の意味を持たないオペランドに使う。

## 6. Envelope / EnvelopeStore

許可された各呼び出しの結果を記録する、ゲートウェイ所有の署名付き観測の
正本。オペランド検証はこのストアから読むため、エージェントが中間値を偽って
報告しても、後続のオペランド検査には影響できない(`ARCHITECTURE.md` §4)。
実装は `pauth/envelope.py`。

このノードで登場する概念:

- **Envelope / 封筒.** 記号的な出所に束縛された具体値であり、それを生成した
  ツールが HMAC で署名する。**不変のスナップショット**を束縛するため、共有
  環境状態が後から変更されても、先行する署名は無効にならない。
- **出所 / provenance.** 値がどこから来たか(プロンプト直書き、tool
  フィールド、計算値、構造化、散文からの LLM 抽出)。二つの認可経路
  (ノード 5)の分岐条件であり、`verifiable=False` は人間確認経路行きを
  意味する。

## 7. Plan 実行とツール供給源(runner / SuiteSpec)

生成された `run()` の実行と、許可された call を実際に走らせる側。ツールは
差し替え可能なプロバイダ(`SuiteSpec`: 買い物デモ、AgentDojo、MCP、
OpenAPI)が供給し、実行はゲートウェイ側の `suite.runner` が行う。実装境界は
`pauth/suites/base.py`。

このノードで登場する概念:

- **Crash / クラッシュ.** 生成された `run()` が、実行中に **`_Denied` 以外の
  何らかの Python 例外**を送出し(散文への添字アクセスによる KeyError、
  `datetime <= str` の TypeError、`None.field`)、途中で停止した。データに
  対する*生成コード側*の見立ての欠陥である。セキュリティ事象では**ない**。
  発生場所は plan の実行時であり、コンパイル段階(ノード 3〜4)ではない —
  文法・コンパイルの失敗は文法棄却(ノード 2)ないし
  `SYNTHESIS_POLICY_COMPILED` の不合格(第 2 部)として別に数える。
- **Tool error / ツールエラー.** 模擬ツール自体が例外を送出した。ラッパーが
  それを飲み込み、`None` を返す(`tool_errors` に記録)— クラッシュでは
  *ない*。(ただし、その後 `None.field` を行うコードはそこでクラッシュ
  する。)認可の*後*に起きる失敗であり、Enforcer の拒否とも別物。
- **SuiteSpec.** ツール供給源の契約(`tools`、`make_env`、`runner_factory`)。
  PAuth の中核は、背後のツールがどのプロバイダに由来するかを知らない。

## 失敗の対照表(ノード横断 — 混同しないこと)

似て見える失敗は、発生ノードで区別する。

| 失敗 | 発生ノード | 定義 |
|---|---|---|
| Grammar rejection / 文法棄却 | 2 GrammarValidator | Planner 失敗。評価ファネルでは Planner 側に計上 |
| Crash / クラッシュ | 7 Plan 実行 | 生成コードの `_Denied` 以外の例外。セキュリティ事象ではない |
| Denial / 拒否 | 5 Enforcer | 一致 rule なしによる遮断。クラッシュとは数えない |
| Tool error / ツールエラー | 7 ツール供給源 | 認可後にツール自体が失敗。`None` に化ける |
| Excess / Missing | (ノードではなくトレース比較) | 第 2 部「トレース比較の語」を参照 |

---

# 第 2 部 — ノード軸に載らない語彙

## 評価の分類体系

PAuth は、過不足なく動作を自動認可することを目指す。過剰と欠落は、一つの認可
忠実性の比較における二つの誤りの方向であり、統一的な視点で扱うべきもの。

### 観測モデル

ベンチマークは有限の必須参照トレースを与え、模擬実行は有限の許可トレース
を生成する。

```text
G = benchmark reference calls
R = calls permitted on one concrete generated-plan execution
```

`G` と `R` は多重集合として比較する。重複する call は別々に数えるが、相対
順序は採点しない。参照トレースのアダプタは、欠落側・過剰側の双方でツール名
+制御オペランド(ノード 3)を用いる。ベンチマークが utility 検査を提供する
場合、制御以外の内容は `OUTCOME_TASK_COMPLETED` で別に評価する。

コンパイル済み policy(ノード 4)は別物である。

```text
P(h, call) = the policy decision for a call under execution history h
```

`P` は、guard、ループ、署名付き envelope が関わる、履歴依存の認可関係で
ある。現在のベンチマークは `P` を列挙しない。したがって、トレース指標を
`POLICY_OVER_GRANT`、`POLICY_UNDER_GRANT`、`POLICY_EXACT_GRANT` と呼んでは
ならない。

### 前提条件と生成

- **FEASIBILITY_EXPRESSIBLE / 表現可能性.** 副作用を持つ call の必須制御
  オペランドのそれぞれを、制限された機構で生成できるか。これは機構を考慮
  した経験則であり、Planner が成功するかどうかとは独立である。
- **SYNTHESIS_POLICY_COMPILED / policy生成成功.** 生成コードが文法検証、
  スライシング、ルールコンパイル(ノード 2〜4)を通過したか。コードの欠落や
  無効は失敗となる。

### 実行時および plan-policy の診断

- **RELIABILITY_RUNTIME_CRASH_FREE / 実行時クラッシュなし.** 生成された
  plan が、許可を緩めた使い捨ての模擬実行を Python 例外なしで完走したか。
  このプローブでは執行を無効化してあり、早期の拒否が後段のコードクラッシュ
  を隠すことはない。ツールエラー自体は `None` を返す。その `None` の誤用は
  クラッシュしうる。
- **CONFORMANCE_PLAN_TRACE_PERMITTED / plan-policy整合.** 執行ありの模擬
  実行の間、コンパイル済み policy が、観測された生成 plan のトレースを拒否
  せずに済んだか。これは一往復の具体的な確認であり、あらゆる分岐や履歴に
  わたる完全性の証明ではない。

### 参照に対する認可忠実性

- **REF_REQUIRED_CALLS_PERMITTED / 必要call充足.** 共通のツール+制御オペ
  ランド照合器のもとで、`R` が `G` の必須 call をすべて含むか。これは
  「欠落なし」の半分である。
- **REF_NO_EXCESS_CALLS_PERMITTED / 過剰callなし.** 同じ照合器のもとで、
  `R` が `G` を超える不一致 call を含まないか。これは「過剰なし」の半分で
  ある。
- **REF_EXACT_AUTHORIZATION / 過不足なし.** 先行する二つの指標がともに合格
  したときに限り、合格する。

  ```text
  REF_EXACT_AUTHORIZATION
    = REF_REQUIRED_CALLS_PERMITTED
      AND REF_NO_EXCESS_CALLS_PERMITTED
  ```

空の観測トレースは「過剰なし」の半分には合格しうるが、必須 call の網羅では
不合格となる。必須 call をすべて含み、かつ余分な call も含むトレースは、
その逆の結果になる。両者の連言だけが、観測トレース上の過不足のない一点を
捉える。すべてを許可するコンパイル済み policy であっても、具体的な実行が
参照 call のみを試行すれば合格しうる。その policy 全体にわたる過剰許可を
検出するには、上述の別個の policy 空間のオラクルが必要である。

### 結果、攻撃プローブ、費用

- **OUTCOME_TASK_COMPLETED / タスク結果.** 実行後の環境が、ベンチマークの
  `utility()` 検査に合格したか。参照は目標への唯一の経路とは限らず、正しい
  call が正しい内容や最終状態を保証するわけでもないため、参照忠実性とは
  乖離しうる。
- **AUX_INJECTIONS_DENIED / 固定攻撃call拒否.** ベンチマークでラベル付け
  された強制攻撃 call は拒否されたか。これは固定の試験済み集合に関する証拠
  であり、未知の policy 外 call がすべて拒否されるという証明ではない。
- **COST_TOOL_CALLS / call費用.** コンパイル済み plan あたりの許可 call 数の
  平均。欠落または無効な plan は、この費用の分母から除外する。

### 診断上の原因であり、最上位の軸ではない

- **Not attempted / 未試行.** 必須 call が、plan からもエージェントからも
  一度も出されなかった。
- **Attempted but denied / 試行したが拒否.** 必須 call は出されたが、Enforcer
  がそれを止めた。
- **Crash before call / call前クラッシュ.** 生成コードが必須 call に到達
  する前に失敗した。
- **Tool error / ツールエラー.** 許可されたツールが、認可の後に失敗した。

いずれも参照 call の欠落につながりうるが、原因を区別せずにそれらを Enforcer
に帰属させるのは誤りである。執行ありの実行は最初の拒否で停止するため、現在
の集計は `NOT_ATTEMPTED` を独立した指標としては報告しない。正確な原因帰属
には、別個の試行トレースが必要になる。

### 旧名称の対応表

記録済みの実験を書き換えないため、過去のログには旧名称が残っている。現在の
評価では、次のように読み替える。

| 旧名称 | 現在の指標または分解 |
|---|---|
| `AVAIL_1_EXPRESSIBLE` | `FEASIBILITY_EXPRESSIBLE` |
| `AVAIL_2_PLAN_VALID` | `SYNTHESIS_POLICY_COMPILED` |
| `AVAIL_3_RAN_CLEAN` | 実行時クラッシュなしと plan-トレース整合に分割 |
| `AVAIL_4_CALLS_MADE` | `REF_REQUIRED_CALLS_PERMITTED` |
| `SEC_NO_EXCESS_CALLS` | `REF_NO_EXCESS_CALLS_PERMITTED` |
| なし | `REF_EXACT_AUTHORIZATION` |

パラメータ化された単一の `eval/funnel.py` が、各枠組みについて測定可能な
部分集合を報告する。`eval/gates.py` は、キャッシュ済みの AgentDojo plan に
ついて同じ分類体系を出力する。

## トレース比較の語

`G` 対 `R` の比較(上述)で使う語。どのノードにも属さず、観測トレースの
性質である。

- **Excess / 過剰.** 参照トレースと一致しない許可 call。
- **Missing / 欠落.** 許可トレースと一致しない参照 call。plan の抜け、拒否、
  クラッシュのいずれもが、観測上は同じ欠落 call を生みうる(発生ノードの
  区別は第 1 部「失敗の対照表」)。

## ベンチマーク由来の語

- **ground_truth() / 正解call列.** タスクに対する AgentDojo の正準の正しい
  ツール呼び出し列。実現可能性の経験則と、参照忠実性の両半分を可能にする。
  これはベンチマークの参照であり、他に妥当なトレースが存在しないことの証明
  ではない。
- **utility() / 目標達成判定.** 実行後の環境に対する AgentDojo の決定的な
  検査: 目標状態に到達したか。参照忠実性とは別に、`OUTCOME_TASK_COMPLETED`
  として報告される。
- **Framework coverage / 枠組みの網羅範囲.** 各ベンチマークは、gate の連鎖
  の*一部*しか動かさない。`ground_truth` と `utility` の両方を備えるのは
  AgentDojo だけであり、参照忠実性+結果の完全な比較を支えられるのも
  AgentDojo だけである。InjecAgent と強制プローブは、主に固定のラベル付き
  攻撃事例を供給する。

## 測定上の知見

- **Measurement noise / 測定雑音.** キャッシュなし(`--no-cache`)で新規に
  測った agentic な受理率は、実行ごとに ±5–7 pt の変動を伴う(gpt-4.1 の
  非決定性)。小さな効果を見るには、複数回実行の平均か、決定的な(キャッシュ
  済み/正解データによる)指標が必要である。
