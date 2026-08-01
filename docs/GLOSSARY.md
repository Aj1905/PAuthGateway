# 用語集

`pauth/`、`gateway/`、`eval/`、および設計文書の全体で用いる語彙の正確な定義。
用語はコードに根ざしている。区別が微妙な箇所では、正確な境界を明記する。

**本書の構成.** 語彙を四つの軸に分ける。

1. **第 1 部 — ノード**: パイプラインを構成する部品(空間的な構成要素)と、各ノードに固有の機構・挙動。
2. **第 2 部 — ノード間を流れる情報形式**: 各ノードが生成・消費する成果物を、パイプラインの時系列順に定義する。
3. **第 3 部 — 評価指標**: 観測モデルと指標の分類体系。
4. **第 4 部 — その他**: ベンチマーク由来の語、測定上の知見。

まず冒頭の全体図が、ノードと情報の対応を一望する。

---

# 全体図 — ノードと、その間を流れる情報

PAuth の内部実装をノードで簡略化した見取り図。角括弧のノードは第 1 部で、
矢印上を流れる情報 (a)–(k) は第 2 部で定義する。実装ファイルとの対応、
ゲートウェイの外側(hooks / HTTP / 配備形態)は `ARCHITECTURE.md` を正とする。

```text
ユーザー
  │ (a) prompt
  ▼
[1 Planner] ◄──────────────────┐
  │ (b) run() コード            │ (b') 文法棄却の理由
  ▼                            │     (agentic 再生成ループ)
[2 GrammarValidator] ───────────┘
  │ (c) 検証済み run() コード
  ▼
[3 Slicer]
  │ (d) スライス(オペランド式 + guard)
  ▼
[4 Rule compiler]
  │ (e) ルール(その全体がコンパイル済み policy)
  ▼
━━━ ここまで計画時: タスクにつき一度だけ ━━━
━━━ ここから実行時: ツール呼び出しごと ━━━

エージェント(plan の実行)
  │ (f) tool call(ツール名 + 具体オペランド)
  ▼
[5 Enforcer] ◄─ (g) 既存 envelope の参照 ─ [6 EnvelopeStore]
  │
  ├─ 出所が証明可能 ──► 自動認可: 許可 / 拒否(_Denied)
  └─ 出所が検証不能 ──► (h) PendingConfirmation ──► Confirmer(人間の承認/却下)
  │
  │ (i) 許可された call のみ
  ▼
[7 ツール供給源 (runner)] ─► 実ツール実行
  │ (j) 実行結果
  ▼
[6 EnvelopeStore] ← (k) HMAC 署名付き envelope として記録
```

| 区間 | 流れる情報 | 定義 |
|---|---|---|
| ユーザー → Planner | (a) prompt — 汚染されていない自然言語タスク | 第 2 部 |
| Planner → GrammarValidator | (b) run() コード — 制限文法に従う plan | 第 2 部 |
| GrammarValidator → Planner | (b') 文法棄却の理由 — agentic 再生成の入力 | 第 1 部ノード 2 |
| GrammarValidator → Slicer | (c) 検証済み run() コード | 第 2 部 |
| Slicer → Rule compiler | (d) スライス — オペランド式 + guard | 第 2 部 |
| Rule compiler → Enforcer | (e) ルール — 全体でコンパイル済み policy | 第 2 部 |
| エージェント → Enforcer | (f) tool call — 検査の単位 | 第 2 部 |
| EnvelopeStore → Enforcer | (g) 既存 envelope — オペランド・guard の照合材料 | 第 2 部 |
| Enforcer → Confirmer | (h) PendingConfirmation — 検証不能オペランドの保留 call | 第 2 部 |
| Enforcer → runner | (i) 許可された call のみ — 自動認可または人間の承認を経たもの | 第 1 部ノード 5 |
| runner → EnvelopeStore | (j)(k) 実行結果 → HMAC 署名付き envelope | 第 2 部 |

---

# 第 1 部 — パイプラインのノード

## 0. 系全体

- **PAuth パイプライン.** 自然言語プロンプトから実行時の認可判定までの中核
  連鎖(冒頭の全体図)。本書のノード軸はこの連鎖の処理順に従う。確認ゲート・
  `PendingConfirmation`・`Confirmer` は独立ノードではなく、Enforcer
  (ノード 5)の人間確認経路を構成する部品である。runner に到達するのは、
  自動認可または人間の承認を経た call だけである。Planner(ノード 1)だけが
  非決定的(LLM)になりうる。ノード 2 以降はすべて決定的である。実装の対応:
  `pauth/codegen.py`(Planner プロンプト)、`pauth/grammar.py`、
  `pauth/slicing.py`、`pauth/rules.py`、`pauth/enforcer.py`、
  `pauth/envelope.py`、`pauth/suites/base.py`。

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
  「計画時/実行時」の二相で書くこと。同名衝突に注意: 旧 `issues`
  追跡の B 番号(現在は `DESIGN_STATUS.md`「開発上のボトルネック」#1–#5)は
  まったく別の名前空間であり、文書中では「旧 `issues` B?」「ボトルネック #n」
  と明示して書く。論文ラベルを B5 以降へ勝手に拡張しないこと(確認ゲートを
  「Gate B5」と呼ぶ旧用法は廃止済み)。

## 1. Planner / 計画器

プロンプト+ツールスキーマを入力に、制限文法(第 2 部)に従う `run()` 関数を
出力する。LLM を使いうる唯一のノードであり、ノード 2 以降は決定的である。
Planner 自体は戦略によって決定的にも非決定的にもなる。境界と正準名の実装は
`gateway/planning/planner.py`、LLM 版は `gateway/planning/agentic_planner.py`。

このノードの機構・挙動:

- **製品レベルの Planner 戦略名.** `PAUTH_PLANNER_STRATEGY` や wire 設定で
  選ぶ名前。正準集合と生成処理は `gateway/planning/planner.py` の
  `KNOWN_STRATEGIES` と `build_planner()` を正とする。
- **PlanGenerationError / 計画生成エラー.** Planner の選択または生成が
  `PlanDraft` を返せず、Enforcer とルールを作れなかったこと。AgentChannel は
  プロンプトを不受理として返す。GrammarValidator の文法棄却(ノード 2)や、
  Enforcer の拒否(ノード 5)とは別の失敗である。
- **評価用の生成モード.** `eval/` のランナーが比較実験のために使う選択肢。
  `oneshot` は一回生成で終わり、`agentic` は文法棄却理由と実行時フィードバックを
  LLM に返して再生成する。製品レベルの Planner 戦略名とは別の名前空間である。
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
  (第 4 部)の方が望ましい。
- **Prose-locked value / 散文に埋もれた値.** 非構造化テキストの返り値の中に
  しか存在しない値(`.txt` の中の請求金額)であり、文字列演算を持たない文法
  では抽出できない。表現可能性(`FEASIBILITY_EXPRESSIBLE`、第 3 部)の失敗の
  主因。

## 2. GrammarValidator / 制限文法検証器

Planner 境界の契約の執行点。Planner の出力コードが制限文法(第 2 部)に適合
するかを、構文検査(`parse_and_validate`)→ 死コード除去
(`strip_dead_code`)→ 意味検査(`validate_semantics`)の順で検査する。
実装は `pauth/grammar.py` の一箇所だが、呼び出し側は Planner 側のリトライ
ループと `pauth.prepare()` 内の再検証の二つある(`ARCHITECTURE.md` §1.1)。

このノードの機構・挙動:

- **Grammar rejection / 文法棄却.** 検証器がコードを拒絶すること。Enforcer の
  拒否(ノード 5)とは別の失敗クラス(Planner 失敗)として評価ファネルに
  計上される。agentic 経路では棄却理由が LLM に返され、再生成の入力になる。

## 3. Slicer / スライサ

決定的。検証済みの `run()` から、ツール呼び出しごとの**スライス**(第 2 部)
を導出する。実装は `pauth/slicing.py`。

## 4. Rule compiler / ルールコンパイラ

決定的。スライスを**ルール**(第 2 部)にコンパイルする(論文 Algorithm 1)。
実装は `pauth/rules.py`。ここまで(ノード 1〜4)がタスク開始時に一度だけ
走り、以降は実行時執行(ノード 5〜7)に移る。

## 5. Enforcer / 執行器

実行時の執行点。各ツール呼び出しを横取りし、ルールに照らして検査し
(rule が存在し、guard が成立し、すべてのオペランドがスライスされた値に
等しい)、許可されれば実行させ、結果を署名付き envelope(第 2 部)に包む。
実装は `pauth/enforcer.py`。

このノードの機構・挙動:

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
    証明が*できず*、call は `PendingConfirmation`(第 2 部)として保留され、
    人間に回される。人間の承認/却下がそのまま認可であるため、誤った確認は
    過剰許可に、保守的な確認は過少許可になりうる。ヘッドレス実行
    (`Confirmer` なし)では、これらの call は保留のまま → 未完了となる。
    人間を介した配備(`InteractiveConfirmer`)では完了できる。
- **Confirmation gate / 確認ゲート.** 信頼できない由来の値が、副作用のある
  call の制御オペランドに達したとき、人間の確認のために call を保留する
  機構。人間確認経路の実装点。コード: `_confirmation_gate` →
  `PendingConfirmation` → `Confirmer`。
- **Free operand / 自由オペランド.** 配備者が `PolicyAwareEnforcer`
  (`gateway/runtime/policy.py`)で `(tool, parameter)` の組に印を付け、
  オペランド検査を飛ばす対象。検索クエリや自由記述のメッセージ本文など、
  取引上の意味を持たないオペランドに使う。

## 6. EnvelopeStore / 封筒保管庫

許可された各呼び出しの結果(envelope、第 2 部)を記録する、ゲートウェイ所有
の署名付き観測の正本。オペランド検証はこのストアから読むため、エージェントが
中間値を偽って報告しても、後続のオペランド検査には影響できない
(`ARCHITECTURE.md` §4)。署名の鍵束はゲートウェイが所有する(署名の根は
一つ)。実装は `pauth/envelope.py`。

## 7. Plan 実行とツール供給源(runner / SuiteSpec)

生成された `run()` の実行と、許可された call を実際に走らせる側。ツールは
差し替え可能なプロバイダが供給し、実行はゲートウェイ側の `suite.runner` が
行う。実装境界は `pauth/suites/base.py`。

このノードの機構・挙動:

- **SuiteSpec.** ツール供給源の契約(`tools`、`make_env`、`runner_factory`)。
  買い物デモ、AgentDojo、MCP、OpenAPI が実装する。PAuth の中核は、背後の
  ツールがどのプロバイダに由来するかを知らない。
- **Crash / クラッシュ.** 生成された `run()` が、実行中に **`_Denied` 以外の
  何らかの Python 例外**を送出し(散文への添字アクセスによる KeyError、
  `datetime <= str` の TypeError、`None.field`)、途中で停止した。データに
  対する*生成コード側*の見立ての欠陥である。セキュリティ事象では**ない**。
  発生場所は plan の実行時であり、コンパイル段階(ノード 3〜4)ではない —
  文法・コンパイルの失敗は文法棄却(ノード 2)ないし
  `SYNTHESIS_POLICY_COMPILED` の不合格(第 3 部)として別に数える。
- **Tool error / ツールエラー.** 模擬ツール自体が例外を送出した。ラッパーが
  それを飲み込み、`None` を返す(`tool_errors` に記録)— クラッシュでは
  *ない*。(ただし、その後 `None.field` を行うコードはそこでクラッシュ
  する。)認可の*後*に起きる失敗であり、Enforcer の拒否とも別物。

## 失敗の対照表(ノード横断 — 混同しないこと)

似て見える失敗は、発生ノードで区別する。

| 失敗 | 発生ノード | 定義 |
|---|---|---|
| Grammar rejection / 文法棄却 | 2 GrammarValidator | Planner 失敗。評価ファネルでは Planner 側に計上 |
| Crash / クラッシュ | 7 Plan 実行 | 生成コードの `_Denied` 以外の例外。セキュリティ事象ではない |
| Denial / 拒否 | 5 Enforcer | 一致 rule なしによる遮断。クラッシュとは数えない |
| Tool error / ツールエラー | 7 ツール供給源 | 認可後にツール自体が失敗。`None` に化ける |
| Excess / Missing | (ノードではなくトレース比較) | 第 3 部「トレース比較の語」を参照 |

---

# 第 2 部 — ノード間を流れる情報の形式(タイムライン順)

各ノードが生成・消費する成果物。パイプラインの時系列順に並べる。
「どのノードが作り、どのノードが読むか」を各項に明記する。

## 計画時(タスクにつき一度)

- **Prompt / ユーザープロンプト.** 汚染されていない自然言語のタスク。
  エージェントがいかなるツール出力を読むよりも*前に*捕捉される。Planner
  (ノード 1)の唯一の入力であり、計画は一度だけなので、この情報は
  セッションに一度しか流れない。
- **run() コード / plan.** Planner が生成する、単一の命令型関数。制限文法に
  適合しなければならない。GrammarValidator(ノード 2)が検証し、Slicer
  (ノード 3)が消費する。実行時には plan として走り、そのツール呼び出しが
  Enforcer を通る。
- **Restricted grammar / 制限文法.** run() コードの形式仕様: 狭い Python
  部分集合(メソッド呼び出しなし、文字列演算なし、`while` なし、有界の
  `for`、flat/nested-if ≤3)。スライシングとルールコンパイルを解析可能に
  保つために狭くしてあり、同じ制限が `FEASIBILITY_EXPRESSIBLE`(第 3 部)の
  上限を定める(論文付録 A)。
- **Slice / スライス.** ツール呼び出しごとの記号的な仕様: 各オペランドの式
  +その call に到達するための経路条件(guard)。Slicer(ノード 3)が生成し、
  Rule compiler(ノード 4)が消費する。
- **Guard / ガード.** スライスの構成要素。call に対する経路条件(`if C:` →
  guard `C`。入れ子 → `C1 and C2`。else → `not C`)。Enforcer は、すべての
  guard の成立を要求する。
- **Control operand / 制御オペランド.** 副作用のある call の効果を決定する
  オペランド(送金先、金額、宛先など)。スライスが式として拘束する対象で
  あり、評価の参照照合(ツール名+制御オペランド、第 3 部)でも比較の単位に
  なる。対概念は内容オペランド(自由オペランド、ノード 5)。
- **Rule / ルール.** コンパイル済みのスライス。Rule compiler(ノード 4)が
  生成し、Enforcer(ノード 5)が実行時に具体的な call を照合する対象。
  いずれかの rule に一致すればよい意味論(any-match)。
- **コンパイル済み policy.** あるタスクについてコンパイルされたルールの
  全体。履歴依存の認可関係 `P(h, call)`(第 3 部「観測モデル」)を定める。
  一回の具体的な実行が観測するのはその一断面にすぎない。

## 実行時(ツール呼び出しごと)

- **Tool call / ツール呼び出し.** エージェントが実行時に発行する具体的な
  呼び出し(ツール名+具体オペランド)。Enforcer(ノード 5)の検査の単位で
  あり、許可されたものだけが runner(ノード 7)に届く。
- **Envelope / 封筒.** 記号的な出所に束縛された具体値であり、それを生成した
  ツールが HMAC で署名する。runner の実行結果から作られ、EnvelopeStore
  (ノード 6)に記録され、後続のオペランド検査が読む。**不変のスナップ
  ショット**を束縛するため、共有環境状態が後から変更されても、先行する署名は
  無効にならない。
- **出所 / provenance.** 値の属性: どこから来たか(プロンプト直書き、tool
  フィールド、計算値、構造化、散文からの LLM 抽出)。二つの認可経路
  (ノード 5)の分岐条件であり、`verifiable=False` は人間確認経路行きを
  意味する。
- **PendingConfirmation / 保留 call.** 人間確認経路で保留された call の
  記録。確認ゲート(ノード 5)が生成し、`Confirmer` の承認/却下を待つ。
  承認されれば runner へ進み、却下されれば実行されない。ヘッドレス実行では
  保留のまま残る。
- **トレース.** 一回の実行で観測された call の列。評価では、許可トレース
  `R` を参照トレース `G` と比較する(第 3 部「観測モデル」)。

---

# 第 3 部 — 評価指標

PAuth は、過不足なく動作を自動認可することを目指す。過剰と欠落は、一つの認可
忠実性の比較における二つの誤りの方向であり、統一的な視点で扱うべきもの。

## 観測モデル

ベンチマークは有限の必須参照トレースを与え、模擬実行は有限の許可トレース
を生成する。

```text
G = benchmark reference calls
R = calls permitted on one concrete generated-plan execution
```

`G` と `R` は多重集合として比較する。重複する call は別々に数えるが、相対
順序は採点しない。参照トレースのアダプタは、欠落側・過剰側の双方でツール名
+制御オペランド(第 2 部)を用いる。ベンチマークが utility 検査を提供する
場合、制御以外の内容は `OUTCOME_TASK_COMPLETED` で別に評価する。

コンパイル済み policy(第 2 部)は別物である。

```text
P(h, call) = the policy decision for a call under execution history h
```

`P` は、guard、ループ、署名付き envelope が関わる、履歴依存の認可関係で
ある。現在のベンチマークは `P` を列挙しない。したがって、トレース指標を
`POLICY_OVER_GRANT`、`POLICY_UNDER_GRANT`、`POLICY_EXACT_GRANT` と呼んでは
ならない。

## 前提条件と生成

- **FEASIBILITY_EXPRESSIBLE / 表現可能性.** 副作用を持つ call の必須制御
  オペランドのそれぞれを、制限された機構で生成できるか。これは機構を考慮
  した経験則であり、Planner が成功するかどうかとは独立である。
- **SYNTHESIS_POLICY_COMPILED / policy生成成功.** 生成コードが文法検証、
  スライシング、ルールコンパイル(ノード 2〜4)を通過したか。コードの欠落や
  無効は失敗となる。

## 実行時および plan-policy の診断

- **RELIABILITY_RUNTIME_CRASH_FREE / 実行時クラッシュなし.** 生成された
  plan が、許可を緩めた使い捨ての模擬実行を Python 例外なしで完走したか。
  このプローブでは執行を無効化してあり、早期の拒否が後段のコードクラッシュ
  を隠すことはない。ツールエラー自体は `None` を返す。その `None` の誤用は
  クラッシュしうる。
- **CONFORMANCE_PLAN_TRACE_PERMITTED / plan-policy整合.** 執行ありの模擬
  実行の間、コンパイル済み policy が、観測された生成 plan のトレースを拒否
  せずに済んだか。これは一往復の具体的な確認であり、あらゆる分岐や履歴に
  わたる完全性の証明ではない。

## 参照に対する認可忠実性

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

## 結果、攻撃プローブ、費用

- **OUTCOME_TASK_COMPLETED / タスク結果.** 実行後の環境が、ベンチマークの
  `utility()` 検査に合格したか。参照は目標への唯一の経路とは限らず、正しい
  call が正しい内容や最終状態を保証するわけでもないため、参照忠実性とは
  乖離しうる。
- **AUX_INJECTIONS_DENIED / 固定攻撃call拒否.** ベンチマークでラベル付け
  された強制攻撃 call は拒否されたか。これは固定の試験済み集合に関する証拠
  であり、未知の policy 外 call がすべて拒否されるという証明ではない。
- **COST_TOOL_CALLS / call費用.** コンパイル済み plan あたりの許可 call 数の
  平均。欠落または無効な plan は、この費用の分母から除外する。

## トレース比較の語

`G` 対 `R` の比較で使う語。どのノードにも属さず、観測トレースの性質である。

- **Excess / 過剰.** 参照トレースと一致しない許可 call。
- **Missing / 欠落.** 許可トレースと一致しない参照 call。plan の抜け、拒否、
  クラッシュのいずれもが、観測上は同じ欠落 call を生みうる(発生ノードの
  区別は第 1 部「失敗の対照表」)。

## 診断上の原因であり、最上位の軸ではない

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

パラメータ化された単一の `eval/funnel.py` が、各枠組みについて測定可能な
部分集合を報告する。`eval/gates.py` は、キャッシュ済みの AgentDojo plan に
ついて同じ分類体系を出力する。

---

# 第 4 部 — その他

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
