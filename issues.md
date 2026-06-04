# issues

PAuth ゲートウェイ設計の議論と実装の中で見えた未解決課題・既知制約・要レビュー項目のリスト。
1件1件は短く、深掘りは `grill.md` の対応する Q に飛ぶ。

ステータス凡例:

- 🔴 **open** — 未解決、設計か実装かの判断が必要
- 🟡 **partial** — 暫定対処済み、本治療は別途
- 🟢 **mitigated** — 主要対策は実装済み、運用観察フェーズ
- ⚪ **deferred** — 認識済みだが現段階では着手しない

---

## A. PAuth の構造的な限界

### A1. Restricted grammar の表現力不足 🔴

**事象:** `B1_cheapest_under_80` / `ai_free_two_products_pick_cheaper` / `ai_free_post_action_followup` のような「比較してどちらか」「成功したら次へ」を含むタスクは Appendix A の文法に乗らない(else / nested-if / 二重代入の禁止)。

**現状:** LLM がプロンプトの一部を simplification して grammar に合わせるか、retry 上限まで失敗する。

**関連:** Q9, Q12, Q14, A2

**選択肢:** grammar 緩和 / プロンプトの事前分解 / 別形式(IFTTT applet 風)への移行。

### A2. Intent 捕捉の検証層が存在しない 🔴

**事象:** Gateway は「コードが grammar に合うか」「コードと slice / rule が整合するか」しか見ない。**「コードがユーザの intent を捕捉しているか」を疑う層が無い**。結果、LLM が grammar 制約を満たすために intent を勝手に削ったコードでも受理される。

**現状:** fixture を `expected_accept=True` に直して当面の FA を消したが、本物の問題は残る。

**関連:** Q14(PreAuth Grill Layer)

**選択肢:** (1) 別 LLM で intent 差分検出、(2) ユーザに plan を提示して承認、(3) prompt を事前形式化。

### A3. UI プロンプト内 injection は scope 外 ⚪

**事象:** ユーザ自身がプロンプトに `Ignore previous instructions...` を貼った場合、PAuth では弾けない。論文 §3 で明示的に scope 外。

**現状:** Q11 / Q14 で明示化済み。`FREEFORM_OUT_OF_SCOPE` に E1 を分離。

**関連:** Q11, Q14

### A4. Agent forwarding 整合性が新たな信頼前提 🟡

**事象:** AgentChannel 導入で「agent が user prompt を改変せず forward する」前提が新規。

**現状:** Q13 で明示化、AgentChannel が "prompt は最初に1回だけ" "tool call は prompt 後のみ" を構造強制。Claude Code hook (UserPromptSubmit) を使う構成では forward が LLM 起動前なので追加保証あり。

**関連:** Q13

---

## B. Gateway 実装の隙間

### B1. Session 状態が in-memory のみ 🟡

**事象:** `AgentChannel` の session は Python プロセスのメモリ保持。`systemctl restart` で全 session 消失。

**現状:** Phase 1(self-hosted、単一ユーザ)では妥協可。

**関連:** architecture.md §9

**選択肢:** Redis / DynamoDB / Cosmos に外出し。Phase 2 以降の検討。

### B2. OAuth / API キー管理機構が無い 🔴

**事象:** MCP プロセスが自分で credential を抱える前提。複数ユーザに開く時、ユーザ単位の credential 隔離・rotation・audit を gateway が引き受ける必要が出る。

**現状:** Phase 1 では問題なし。

**関連:** architecture.md §9.2

**選択肢:** Secrets Manager / Key Vault バックエンドの adapter。

### B3. Grill UI 未実装 🔴

**事象:** Q14 の PreAuth Grill Layer は設計のみ、UI 実装ゼロ。

**現状:** Q14 で G1〜G3 の選択肢を提示、推奨は G2(Claude Code hook の additionalContext 機構)。

**関連:** Q14, Q14-a 〜 Q14-d

### B4. Stdio MCP subprocess のヘルスチェック / 再起動なし 🟡

**事象:** `StdioTransport` は subprocess 1本を握りっぱなし。落ちたら次の呼び出しで失敗するだけ。

**現状:** `close()` での terminate は実装済み、再起動ロジックは無い。

**選択肢:** ヘルスチェック付き再起動 / supervisor 抽象。

### B5. Bash escape hatch 🔴

**事象:** Claude Code の `Bash` ツールはあらゆる外部 I/O・破壊操作にアクセスできる。Gateway は MCP 経由しか enforce しない。

**現状:** Hook 構成では `PreToolUse` で `Bash` も拾えるが、コマンド単位のパターンマッチが必要(Q4, Q7 で議論済み)。

**関連:** Q4, Q7

**選択肢:** コマンド allowlist / sandbox / file system 仮想化。

---

## C. テストデータ / fixture

### C1. AI 生成 fixture の expected_accept は信頼できない 🟡

**事象:** `tests/fixtures/ai_generated/` の expected を AI(私)が推測で埋めた。emoji ケースは実測で間違いと判明した。

**現状:** 該当 fixture を実測値に合わせて修正済み、note に明示。残りの AI fixture も同様の review が必要。

**関連:** ユーザ会話 2026-06-04(fixture review)

**残:** 他の AI fixture を1件ずつ人間 review。

### C2. L3 reference data の重複 🟡

**事象:** `pauth/suites/shopping.py` の `_TASKS` が L3 相当データを抱えているが、`tests/fixtures/l3_references.py` には型しか無く `AI_REFERENCES` だけが具体例。歴史的データの移行が library→tests 依存逆転になるため保留中。

**現状:** Suite data 層の切り出しを将来の課題として温存。

**選択肢:** `pauth/suites/shopping/` をパッケージ化して tools(library)と tasks(fixture)を分離。

### C3. Fixture review tooling 無し 🔴

**事象:** AI fixture を1件ずつ人間が見直す作業は現状手作業。

**選択肢:** Web UI / TUI で「次の fixture を見る、expected を直す、note を編集する」サイクルを高速化。

---

## D. 規模拡張時に顕在化する課題

### D1. Plan 生成の token 膨張 🟡

**事象:** 登録 MCP が増えると A1 prompt に全 tool schema が乗り、token 数が線形に膨らむ。

**現状:** `gateway/suite_filter.py` のキーワードベース filter で対処。10〜20 suite までは効くが、それ以上では精度が落ちる。

**選択肢:** Embedding-based filter / LLM filter。

### D2. Multi-suite tool 名衝突 🟡

**事象:** `merge_suites` は衝突時に `ValueError`。Gmail と Slack に同名 `send_message` があれば登録できない。

**現状:** 衝突は登録時にしか起きないので運用回避可。Production では namespacing が要る。

**選択肢:** `<suite>:<tool>` の自動 prefix。

### D3. 大きい tool 戻り値が envelope store を圧迫 🟡

**事象:** MCP が大きな JSON を返すと、`EnvelopeStore` にコピー、tool 戻り値にも保持で重複保持。

**現状:** 観察のみ。実害は未確認。

**選択肢:** 戻り値の `flatten` + symbolic 化、もしくは大きい blob は参照渡し。

### D4. AgentDojo は重い optional dependency 🟡

**事象:** `tests/experiment/agentdojo_adapter` 経由で pydantic / datasets が引っ張られる。

**現状:** import 遅延化済み、本番では使わない予定。

---

## E. 仕様の曖昧さ / 規約

### E1. MCP tool の operand 検証深度が tool 任せ 🟡

**事象:** MCP の `inputSchema` がネスト / Union を含む場合、`ToolDoc` への変換が best-effort で、operand-level 認可が荒くなる。

**現状:** `PolicyAwareEnforcer` で「free 宣言したパラメータは検証スキップ」を提供。`operand_policy` を config に書く。

**関連:** `gateway/policy.py`

**残:** 「どの operand を free と宣言すべきか」のレコメンド / 自動判定。

### E2. Recognizer の決定的サブセットが狭い 🟡

**事象:** `recognize_prompt` は 4 つの hardcoded 正規表現のみ。少しでも phrasing が違うと reject。

**現状:** 該当しない prompt は freeform 経路に流す設計。

**選択肢:** 正規表現拡充 / templates DSL。

### E3. Pre-tool hook の strict / log モード切替の運用ガイド 🟡

**事象:** `GATEWAY_MODE_TOOL=log` で常用すると enforcement が log-only に縮退する。

**現状:** `gateway/hooks/README.md` で説明済み、運用 default は `log`。

**残:** `strict` に切り替えるべき judgement criteria の明文化。

---

## F. 永続研究テーマ(完了しない、継続的に最適化)

### F1. Judge 機構の最適化 ♾️

**性質:** これは1回直して終わる性質の課題ではなく、**プロジェクトの存続期間中ずっと改善対象** となるオープンエンドな研究テーマ。

**スコープ:**

- 判定 prompt の改善(rubric 軸の追加、強さ調整、CoT 強制 等)
- 判定モデルのスイッチ(Claude opus / sonnet / haiku、別ベンダー、複数モデルのアンサンブル)
- 判定構造そのものの変更(単一 LLM call → multi-step / multi-agent / 検証専用 fine-tuned モデル、等)
- 判定対象の workload 設計(難易度、adversarial 含有率、generator モデルの強弱)

**なぜ永続課題か:**

判定の良し悪しは「判定対象の workload」「generator の強弱」「想定される攻撃面」の3変数すべてに依存する。これらが固定されない以上、最適な判定機構も固定されない。具体例:

- generator が強い時に最適な judge は、弱い generator では over-reject する
- adversarial prompt の傾向が変われば検査軸も変える必要
- 新しい threat model(Q14 PreAuth Grill との分担等)が定まれば判定対象範囲も変わる

**実験すべき軸の例(継続更新):**

| 軸 | 試すべき設定例 |
|---|---|
| 判定モデル | claude-opus-4-8, opus-4-7, sonnet-4-6, haiku-4-5, gpt-4o, アンサンブル |
| 判定 prompt 厳しさ | "OBVIOUS mismatch only" / "strict per-clause check" / "demand explicit edge case handling" |
| 判定構造 | 単一 LLM call / step-by-step / generator-judge debate / back-translation |
| Generator 強さ | gpt-4.1 / gpt-4.1-mini / gpt-3.5-turbo / claude haiku |
| Workload 構成 | adversarial 比率 / under-spec 比率 / 多 step task の比率 |

**測定の基本枠組み(都度更新):**

- baseline: 現在の `judge=opus-4-8 + gpt-4.1 generator + 既存 fixture`
- ablation: `--no-judge` で純 grammar 経路と比較
- adversarial: `judge_adversarial_test.py` で reject 率(現在 8/8 = 100%)
- false reject: judge ON で本来 accept すべきが reject される率
- コスト: 1 prompt あたり追加 token / latency

**永続性の現れ方:**

- 結果は1回出して終わりではなく、judge 関連の変更 / 新しい fixture / 新しい threat model が出るたびに **再走** する
- 過去結果は `tests/results/judge_experiments/<date>-<config>.json` 等に蓄積し、いつでも前回比較できるようにする
- "今この設定で最適"の判断は時系列で記録される

**関連 grill:** Q15(導入)、Q14(PreAuth Grill との分担)、Q12(generator 側の retry loop)

---

## 一覧サマリ

| ID | カテゴリ | 状態 | 着手優先度の目安 |
|---|---|---|---|
| A1 | grammar 表現力 | 🔴 | 高(Q14 と一緒に grill) |
| A2 | intent 検証層 | 🔴 | 高(Q14) |
| A3 | UI prompt injection | ⚪ | 低(scope 外宣言済) |
| A4 | agent forwarding 信頼 | 🟡 | 低(明示化済) |
| B1 | session in-memory | 🟡 | 中(Phase 2 で) |
| B2 | OAuth 管理 | 🔴 | 中(multi-user に開く時) |
| B3 | Grill UI | 🔴 | 高(Q14 実装) |
| B4 | stdio supervisor | 🟡 | 中 |
| B5 | Bash escape | 🔴 | 高(Q7) |
| C1 | AI fixture review | 🟡 | 中(継続作業) |
| C2 | L3 重複 | 🟡 | 低 |
| C3 | fixture review tooling | 🔴 | 中 |
| D1 | A1 token 膨張 | 🟡 | 低(suite filter で当面 OK) |
| D2 | tool 名衝突 | 🟡 | 低 |
| D3 | envelope store サイズ | 🟡 | 低 |
| D4 | AgentDojo 依存量 | 🟡 | 低 |
| E1 | MCP operand 深度 | 🟡 | 中(運用しながら) |
| E2 | recognizer 狭さ | 🟡 | 低 |
| E3 | strict / log 運用ガイド | 🟡 | 中 |
| F1 | Judge 機構の最適化(♾️ 永続) | ♾️ | 継続(都度測定) |
