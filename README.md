# PAuth 再現実験

論文 **"PAuth – Precise Task-Scoped Authorization For Agents"**
(Sharma, Jiang, Lin & Chen, arXiv:2603.17170) の再現実装です。

論文の中心的主張 — *NL slice と envelope によるタスクスコープ認可は、benign タスクを
すべて許可し（zero FP）、混入された不正操作をすべて検出する（zero FN）* — を、
実際に計測して検証できる形で再構築しています。

> **計測は正直です。** 実験ランナーは FP/FN を 0 と決め打ちしません。LLM が誤った
> コードを生成すれば FP が出るし、slice が不正確なら FN が出ます。ランナーは
> 起きたことをそのまま報告します（`ANOMALIES` セクション）。

---

## 実験結果（GPT-4.1, AgentDojo v1 + shopping）

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

- **#FP (#benign runs)** — benign 実行のうち、何らかの呼び出しが拒否された
  タスク数（論文の偽陽性）。
- **#FN (#injection runs)** — forced injection のうち PAuth が許可してしまった件数
  （論文の偽陰性）。
- **A1 skipped** — API キー不在やコード生成エラーで評価不能だったタスク数。
- **ANOMALIES** — FP/FN または生成コードのクラッシュが起きたタスクの詳細。
  ここが空であれば zero FP / zero FN が成立しています。

詳細は `experiment/results/results.json`（タスクごとの slice・拒否理由・
トークンコストを含む）に出力されます。

---

## FP/FN の計測方法（なぜ正直と言えるか）

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
experiment/
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
