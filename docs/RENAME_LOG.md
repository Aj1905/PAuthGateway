# 改名記録(暫定・削除予定)

実装途中の方針転換で起こった名前の変更を、一箇所に集めた一時的な記録。
現行名の正本は `docs/SYSTEM_MODEL.md` であり、本書は「旧名 → 新名」の対応
だけを持つ。**全用語の固定が完了した時点で、本ファイルごと削除する。**
それまでは、名前の変更を行ったら必ず本書に一行足すこと。

コミットのハッシュは 2026-08-03 の履歴書き換え後の現行値。

## 用語・指標の改名(時系列)

### 2026-07-12 — 評価指標の語彙統一

- 評価指標名を正準の UPPER_SNAKE 語彙へ統一(01f60b3)。
- `loop_overhead` → `toolcall_eval`(b4a5ec4)。

### 2026-07-18〜19 — 論文記号ラベルの廃止

- `A1` → `Planner`、`A2` → `Slicer`、`A3` → `Rule compiler`、`B1`–`B4` →
  `Enforcer` の記述名へ(c8d0e7d)。
- 用語集の地の文から A1–B4 ラベルを全廃(b561e62)、コードからも全廃
  (e1a7200)。以後 A1–A4 / B1–B4 は論文(2603.17170)Figure 6 の矢印
  ラベルとしてのみ使う。

### 2026-08-02 — 制限文法 → DSL

- 「制限文法 / restricted grammar」→「DSL」(31bfa22)。言語と規則を区別
  しない。二言語命名規約を同時に導入。
- 追補: ノード名を GrammarValidator へ戻し、DSL は言語の呼称に限定
  (77fa515)。
- 版 ID `G1`/`G2` は改名対象外として凍結。Planner プロンプトの字面も凍結。

### 2026-08-02 — 指標 REF_* → GT_*

いずれも定義・値・分母は不変。

- `REF_REQUIRED_CALLS_PERMITTED` → `REF_NO_MISSING_CALLS`(ca6474e)
  → `GT_NO_MISSING_CALLS`(0f48b19)
- `REF_NO_EXCESS_CALLS_PERMITTED` → `REF_NO_EXCESS_CALLS`(ca6474e)
  → `GT_NO_EXCESS_CALLS`(0f48b19)
- `REF_EXACT_AUTHORIZATION` → `GT_EXACT_AUTHORIZATION`(0f48b19)
- GT = 正解(ground truth)。保存済みの実験成果物
  (`tests/experiment/results/`)は当時のキーのまま凍結してある。
  `docs/GT_NO_MISSING_IMPROVEMENT_LOG.md` は新名へ書き換え済み(数値は
  当時のまま)。

### 2026-08-02 — 日本語の地の文の統一

- 「参照」→「正解」(0f48b19)、「参照トレース」→「正解ツール呼び出し列」
  (76f8f73)。
- Missing の訳語「欠落」→「不足」(1b9bda9)。
- 「ヘッドレス」→「人間が確認しない」(319fa0e)。
- 「Plan 実行」→「計画実行」、ToolExecutor の定義重複を解消(a42e7a6)。

### 2026-08-02 — コードのモジュール名をノード名へ

- `pauth/` のモジュール名を SYSTEM_MODEL のノード名に揃え、ToolExecutor を
  分離(5ccbf49)。

### 2026-08-03〜04 — 定義項見出しの三つ組化(未コミット)

- 定義項の見出しを「日本語名 / 英語名 / `実装名`」の三つ組に統一。実装名を
  英語名の位置や本文に書く形を廃止。例:
  「拒否 / denial(`_Denied`)」→「拒否 / denial / `_Denied`」、
  「ground_truth() / 正解ツール呼び出し列」→
  「正解ツール呼び出し列 / ground truth / `ground_truth()`」、
  「utility() / 目標達成判定」→「目標達成判定 / utility / `utility()`」、
  「重要指示攻撃 / `important_instructions`」→
  「重要指示攻撃 / important instructions / `important_instructions`」。
- 「実行試行 / ExecutionAttempt」→「実行試行 / execution attempt」
  (`ExecutionAttempt` という型は実在しないため、実装名なしの二つ組へ)。
- 見出しの限定子を本文へ移動: 保護レベルの「(L0–L3)」、資格情報仲介の
  「(S4)」、順序執行の「(opt-in)」(削除)。
- ノード名「Rule compiler」→「RuleCompiler」(41 箇所: 設計文書・コード
  注釈・README・CLAUDE.md/AGENTS.md・原稿)。他ノード名
  (GrammarValidator、EnvelopeStore など)と同じ一語 CamelCase 表記へ統一。
  実装のモジュール名 `pauth/rule_compiler.py` と関数 `compile_rule()` は
  改名していない。凍結済み実験成果物内の旧表記は当時のまま。
- 実装名を持たない語は第 3 要素を `-` として「日本語名 / 英語名 / -」に
  統一(59 見出し)。あわせて「確認ゲート / confirmation gate /
  `_confirmation_gate`」「実行状態障害 / execution-state failure /
  `ExecutionStateError`」を三つ組へ昇格。例外類型(ノード名・通信形・
  指標名・設定値・Figure 6 ラベル)は従来どおり。

## 凍結している名前(改名しないもの)

- 版番号 ID(`P1`、`G1`、`G2` など)— 登録表の鍵。
- Planner プロンプトの字面。
- 保存済み実験成果物(`tests/experiment/results/`)内のキー — 当時のまま。
- 指標名・ノード名・通信形 — 現行名で固定(正本: SYSTEM_MODEL.md 命名規約)。
