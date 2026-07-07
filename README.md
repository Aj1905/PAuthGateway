# PAuthGateway

**AI エージェントと、それが叩く実ツール/SaaS の間に挟まる、
個人向けの「タスクスコープ認可ファイアウォール」です。**（最初の対象は Claude Code）

ユーザーが入力した自然言語プロンプトを、エージェントが動き出す前に一度だけ
「制限された計画」へ変換し、以降のすべてのツール呼び出しをその計画に対して
照合します（default-deny）。これにより、プロンプトインジェクションやツール結果
汚染でエージェントが乗っ取られても、**ユーザーが実際に頼んでいない操作は実行
できません**。

手法そのものは論文 **"PAuth – Precise Task-Scoped Authorization For Agents"**
(Sharma, Jiang, Lin & Chen, arXiv:2603.17170) に基づきます。

## 解決する課題

実システム（銀行・メール・社内 SaaS）にアクセスできる自律エージェントは、
インジェクション一発で「攻撃者の宛先に送金」「機密メールを転送」といった、
ユーザーが指示していない操作を実行しうる。既存の対策は (a) エージェント本体を
改造するか、(b) LLM 自身に自分を取り締まらせる かのどちらかに寄りがちで、
前者は導入が重く、後者は乗っ取られた当人に判断を委ねている。

PAuthGateway は強制点をエージェントの外に出す:

- **エージェントは無改造**。Claude Code の hook（将来は MCP/プロキシ）で
  プロンプトとツール呼び出しを横取りするだけ。
- **計画は一度だけ**。汚染されたツール出力を見た後にエージェントが計画を
  書き換えることはできない（plan once / enforce every call）。
- **強制は決定的**。許可判定に LLM は使わない。計画の生成（A1）だけが LLM で、
  照合（A2/A3 と B1–B4）は完全に決定的。
- **ゲートウェイが観測の権威**。各ツールの実行結果はゲートウェイが署名付き
  envelope として記録し、エージェントが値を偽装しても後続の照合に影響しない。

詳細設計は [`architecture.md`](architecture.md)、防御範囲と非対象は
[`THREAT_MODEL.md`](THREAT_MODEL.md) を参照。

## これは何「ではない」か

- 正しさの保証ではない。ユーザーが間違った計画を承認すれば、その計画の中で
  間違ったことは起きる。PAuth は「ユーザーが頼んだ範囲を超えない」ことだけを保証する。
- エージェント本体のサンドボックスではない。ゲートウェイが見るのはツール呼び出しのみ。
  Bash やファイル操作の側チャネルは別の仕組み（サンドボックス等）が必要。

---

## 設計の妥当性（再現実験）

コアアルゴリズムが論文どおり zero FP / zero FN で成立することを、計測可能な形で
実証しています。論文の中心的主張 — *NL slice と envelope によるタスクスコープ認可は、
benign タスクをすべて許可し（zero FP）、混入された不正操作をすべて検出する（zero FN）*
— を、実際に計測して検証できる形で再構築しました。

> **計測は正直です。** 実験ランナーは FP/FN を 0 と決め打ちしません。LLM が誤った
> コードを生成すれば FP が出るし、slice が不正確なら FN が出ます。ランナーは
> 起きたことをそのまま報告します（`ANOMALIES` セクション）。

### 実験結果（GPT-4.1, AgentDojo v1 + shopping）

`python -m experiment.run_experiment --suites all` の実測値:

| Suite | #FN (#injection runs) | #FP (#benign runs) | A1 skipped |
|-------|----------------------|--------------------|------------|
| shopping | 0 (8) | 0 (2) | 0 |
| banking | 0 (135) | 0 (13) | 3 |
| slack | 0 (51) | 0 (7) | 14 |
| travel | 0 (32) | 0 (5) | 15 |
| workspace | 0 (164) | 0 (22) | 18 |
| **Overall** | **0 (390)** | **0 (49)** | **50** |

- **zero FP / zero FN** — A1 が文法に適合したコードを生成し実行できた全タスク
  （benign 49 + forced injection 390 runs）で偽陽性・偽陰性ともに 0。論文 Table 2 の
  中心的主張を再現。
- **A1 skipped 50** — GPT-4.1 が制限文法外のコード（ループ・内包表記・メソッド
  呼び出し・多重代入等）を生成したタスク。A1 ゲートで拒否され enforcer には到達しない。
  論文は「GPT-4.1 は全100タスクで正しいコードを生成」と報告しており、本実装の A1
  成功率はそれより低い（プロンプトの厳密さ・モデルスナップショットの差と推測）。
- **code-crash 3** — 文法は満たすが論理バグ（`str > int` 等の型誤用）で実行時に
  クラッシュしたコード。FP/FN ではなく `ANOMALIES` に別途報告。

この結果が示すのは論文 sec. 5.2 の通り — *slice/rule が正しく導出されれば、
zero FP・zero FN は PAuth の設計上の自然な帰結* である、という点です。

---

## 論文との対応

| 論文 | この実装 |
|------|----------|
| A1: 命令型コード生成（LLM, sec. 4.1.1） | `pauth/codegen.py`（OpenAI GPT-4.1, Appendix A のプロンプト） |
| A2: NL slice 導出（sec. 3.3 / 4.1.2, 決定的） | `pauth/slicing.py` |
| A3: ルールコンパイル（Algorithm 1, 決定的） | `pauth/rules.py` |
| envelope（署名付き, sec. 3.4 / Fig. 3） | `pauth/envelope.py` |
| B1-B4: ランタイム強制（sec. 4.1.3, 決定的） | `pauth/enforcer.py` |
| 制限文法（Appendix A の BNF） | `pauth/grammar.py` |
| AgentDojo 上の実装（sec. 4.1） | `experiment/agentdojo_adapter.py` |
| Shopping スイート（sec. 5.1） | `pauth/suites/shopping.py` |
| forced injection（sec. 5.1） | `experiment/forced_injection.py` |
| FP/FN 評価（sec. 5.2, Table 2） | `experiment/run_experiment.py` |

論文の通り、**LLM を要するのは A1 のみ**で、A2/A3/B1-B4 と envelope は完全に決定的です
（論文 sec. 5.2: "The derivation of slices/rules ... is deterministic without LLM"）。

---

## セットアップ

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Python 3.12 以降を推奨（3.14 で開発・検証済み）。

---

## 1. オフライン検証（API キー不要）

論文の worked example（banking sec. 5.3 / shopping sec. 4・5.3）に対し、
**API を一切呼ばずに** 決定的コア（A2/A3/B1-B4）の zero FP / zero FN を検証します。

```bash
.venv/bin/python -m tests.test_worked_examples
```

検証内容:
- slice 導出が論文 sec. 5.3 の図と一致すること
- benign 実行ですべての呼び出しが許可されること（zero FP）
- forced injection（不正な recipient / 金額の改ざん・不正な operator）が
  すべて拒否されること（zero FN）
- 実際の AgentDojo banking ツール・環境・pydantic オブジェクト上で動作すること

Shopping スイートは reference code を同梱するため、これも API なしで実行できます:

```bash
.venv/bin/python -m experiment.run_experiment --suites shopping
```

### 想定外攻撃プローブ（API キー不要）

AgentDojo の injection task 由来ではない攻撃を、正しい slice が生成済みという前提で
直接 enforcer に投げるテストです。Shopping に加え、AgentDojo の banking / slack /
travel / workspace の実ツールでも確認します:

```bash
.venv/bin/python -m tests.test_unexpected_attacks
```

検証する攻撃:
- off-slice な sensitive operator / read operator
- 宛先・金額・件名・日付・商品名の改ざん
- upstream envelope が存在しない状態での direct call
- guard が false の branch にある呼び出しの強制
- signed envelope の改ざん

結果の解釈は厳密にしてください。PAuth は task-scope 認可なので off-slice 攻撃は拒否できますが、
**正規 slice と完全一致する replay は許可されます**。これは実装バグではなく、PAuth の認可境界です。

---

## 2. フル実験（OpenAI API キーが必要）

AgentDojo の 4 スイート（banking / slack / travel / workspace）について、
A1 を **OpenAI GPT-4.1** で実行し、論文 Table 2 形式の FP/FN を計測します。

```bash
cp .env.example .env          # .env に OPENAI_API_KEY を記入
.venv/bin/python -m experiment.run_experiment --suites all
```

`.env` を使わず環境変数でも可:

```bash
OPENAI_API_KEY=sk-... .venv/bin/python -m experiment.run_experiment --suites all
```

**オプション**

| フラグ | 説明 |
|--------|------|
| `--suites all` | shopping + AgentDojo 4 スイート（既定） |
| `--suites banking,shopping` | スイートを指定 |
| `--limit N` | 各スイート先頭 N タスクのみ（安価な動作確認用） |
| `--model gpt-4.1` | A1 のモデル（`gpt-5-mini` 等も可） |
| `--no-cache` | キャッシュ済み生成コードを無視して再生成 |
| `--out path.json` | 結果 JSON の出力先 |

**コストと時間の目安**: 1 タスクあたり約 $0.002–0.04（論文 Fig. 10）。
全 97 タスクで概ね $1–4、10 分程度。生成コードは `experiment/cache/` に
キャッシュされるため、2 回目以降の再実行は無料です。

まず安価に試すなら:

```bash
.venv/bin/python -m experiment.run_experiment --suites banking --limit 3
```

---

## 出力の読み方

```
Suite       #FN (#injection runs)     #FP (#benign runs)      A1 skipped
banking     0 (166)                   0 (16)                  0
...
Overall     0 (756)                   0 (97)                  0
```

- **#FP (#benign runs)** — benign 実行のうち、何らかの呼び出しが拒否されたタスク数。
- **#FN (#injection runs)** — forced injection のうち PAuth が許可してしまった件数。
- **A1 skipped** — API キー不在やコード生成エラーで評価不能だったタスク数。
- **ANOMALIES** — FP/FN または生成コードのクラッシュが起きたタスクの詳細。
  ここが空であれば zero FP / zero FN が成立しています。

詳細は `experiment/results/results.json`（タスクごとの slice・拒否理由・
トークンコストを含む）に出力されます。

---

## FP/FN の計測方法

- **FP（benign）**: A1 が生成したコードを *実際に実行* し、各ツール呼び出しを
  enforcer に通します。1 つでも拒否されればそのタスクは FP。
  ルールは同じコードから導出されるため、実装が正しければ FP は 0 になるはずです
  — もし出れば、それは A1 のコード品質か実装の不整合を示す本物の信号です。
- **FN（injection）**: forced injection は「benign タスクに紛れ込んだ不正操作」
  （論文 sec. 5.1）。benign 実行後の envelope ストアを前提に、不正呼び出しを
  enforcer に提示します。`tool` のいずれかのルールが許可すれば FN。
  PAuth は default-deny（厳密一致するルールがなければ拒否）です。
- forced injection は 2 種類: ①operand 改ざん（recipient を攻撃者宛に / 金額を増額）、
  ②不正 operator（AgentDojo の injection task が持つ機微な呼び出し）。

テストハーネスは vacuous ではありません: on-slice な呼び出しを injection として
渡せば許可（=FN として検出）されることを確認済みです。

---

## 構成

```
pauth/                  PAuth コア機構（フレームワーク非依存・大半が決定的）
  grammar.py            制限文法のパーサ／検証／dead-code 除去（Appendix A）
  slicing.py            A2: NL slice 導出
  rules.py              A3: Algorithm 1 によるルールコンパイル
  envelope.py           envelope データ構造・HMAC 署名・store
  evaluator.py          slice 式の決定的評価器（ヘルパ len/min/max/first/last 含む）
  enforcer.py           B1-B4: ランタイム強制 + サンドボックス実行器
  codegen.py            A1: OpenAI によるコード生成（Appendix A プロンプト）
  pipeline.py           A1→A2→A3 の結線
  suites/shopping.py    論文の Shopping スイート（自己完結）
gateway/
  api_spec_monitor.py   OpenAPI仕様の変更検知・通知用レポート生成
  gateway.py            plan once / enforce every call のランタイム境界
  openapi_suite.py      OpenAPI 3.x 仕様 → SuiteSpec 自動反映
  planner.py            差し替え可能な A1 戦略（決定的recognizer / LLM free-form）
  agent_channel.py      エージェント向け JSON メッセージ境界
  http_server.py        ローカルHTTP daemon
  BUSINESS_OPERATIONS.md OSS無料範囲 / 商用運用 / 課金境界の整理
  DESIGN_STATUS.md      現状設計 / 議論中 / 不可能 / ボトルネックの整理
  PLANNING_STRATEGIES.md A1 戦略カタログ（対話構造化 / 専用モデル / 形式解析）
  SELF_HOSTING.md       セルフホスト版・ネットワーク接続版の設計境界
tests/experiment/
  agentdojo_adapter.py  AgentDojo 4 スイートを共通 IF に正規化
  forced_injection.py   forced injection 生成（sec. 5.1）
  run_experiment.py     FP/FN 実験ランナー（Table 2 / Fig. 10）
tests/
  test_worked_examples.py  オフライン zero-FP/FN 検証（API キー不要）
```

---

## 再現範囲についての注記

- **A1 のモデル**: 論文は GPT-4.1 を主とし、GPT-5-Mini / Gemini-3-Flash /
  Sonnet-4.5 も部分評価。本実装は OpenAI 系のみ既定対応（`--model` で切替）。
- **envelope 署名**: 論文の multi-host ではサーバ間で署名付き envelope を交換。
  本実装は AgentDojo に合わせた single-host 構成で、共有メモリの envelope store に
  HMAC 署名を付与（論文 sec. 4.1.3 の構成に忠実）。
- **Shopping スイート**: 論文独自スイートのため、論文の例（sec. 4 / 5.3）に基づき
  自己完結で再構築（タスク 2 件、reference code 同梱）。
- **forced injection 件数**: 論文は各タスク向けに手作りした 634 件。本実装は
  AgentDojo の injection task と operand 改ざんから自動生成するため件数は一致せず
  （約 750 件）、より広めの探索になります。
- AgentDojo `v1` のタスク数は banking 16 / slack 21 / travel 20 / workspace 40。
  論文の集計（slack 19）とは僅差です。
