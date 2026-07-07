# grill log

PAuth ゲートウェイ設計に関する grill 質問・推奨回答・ユーザ回答の記録。
新しい grill が発生したら下に追記する。

---

## Q2. snapshot か git か

**質問:** あなたの提案を「git に commit する」から「独立 snapshot ストアに記録する」に置き換えれば、commit churn / ブランチ汚染 / pre-commit hook コスト / WIP 固定化 などのコストはほぼ消える。それでも git を使いたい理由は何か?

- (α) snapshot で十分。git である必要はなかった
- (β) 外部監査可能性が欲しい(`git log` で人間が読める)
- (γ) cross-file の semantic checkpoint が欲しい
- (δ) 他の人と共有・協調したい

**私の推奨:** (α)

**理由:** C を選んだ時点で要件は「失敗時に直前の良好状態に戻れること」。per-file snapshot で十分。git を選ぶとコストを払うことになるが、recoverability のためにそのコストを払う理由がない。

**ユーザの回答:** 「snapshot で決定的復元が可能か?」「ロールバック目標を提示してあとは LLM に埋めてもらう方針だと認識しているけど合ってる?」「git の方が決定的だよね」と反問。

→ 説明により次のように収束:
- snapshot 復元は git と完全に等価に決定的(両方 OS 級 file write)
- LLM は介在しない(復元はmechanicalな操作)
- 「git の方が決定的」は誤り。正しくは「git は **atomic** (cross-file 整合性)」が利点

最終的な収束結果: **(γ') Cross-file atomic checkpoint が必要、ただし git そのものではなく agent 専用ブランチを使う**

---

## Q5. Phase tracking の配置場所

**質問:** read→plan→write ループを実現するには、phase tracking をどこに置くか?

- (P1) harness が tracking(Plan Mode の拡張)
- (P2) agent が自己申告
- (P3) 専用 sub-agent を切り出す

**私の推奨:** (P1)、ただし大幅な harness 改造が必要

**ユーザの回答:** 直接回答なし。「Claude Code の中身は変えたくない、アタッチメントが欲しい」という方向転換へ。

---

## Q6. Agent 出力 → run() ブリッジ方式

**質問:** Claude Code は run() を持たない。PAuth は run() を入り口にする。どう繋ぐか?

- (M1) Claude Code に plan を upfront に吐かせる
- (M2) 自由実行 + capability scope での gate
- (M3) Hybrid: declare → execute → 再 declare
- (M4) 後付け解釈(post-hoc translation)

**私の推奨:** (M1) をベースに、replan が必要な時だけ (M3) を限定的に許す

**ユーザの回答:** 直接回答なし。LLM が PAuth と独立に動く構造への展開へ。

---

## Q8. 実装スコープ確認(3問同時 — AskUserQuestion)

**質問1:** 実装対象の「実ツール層」は?
- AgentDojo ツール suite を使う(推奨)
- 自前のミニ tool suite を新規定義
- 両方サポート

**質問2:** エージェントは何にするか?
- ハードコードされた scenario runner(推奨)
- 実 LLM(GPT-4.1 等)に prompt+injection を投げて自由に動かす
- 両方

**質問3:** 「user → gateway 直結」をどう表現するか?
- Python API として明示的に分離(推奨)
- HTTP/gRPC サーバとして実装
- 今は API レベルの構造保証のみ、network 化は後回し

**ユーザの回答:** すべて推奨選択(AgentDojo / scenario runner / Python API 分離)

→ 実装完了。8 シナリオ・24 attempts すべて期待通り。

---

## Q9. A1(prompt → run())の戦略

**質問:** ゲートウェイの中で run() を誰が生成するか?

- (L1) Deterministic recognizer のみ(現実装)
- (L2) LLM 翻訳 + deterministic verifier(`build_llm_translator` + `verify_run`)
- (L3) LLM 翻訳 + 緩い verifier(formal guarantee を緩める方向)

**私の推奨:** 現状の (L1) で確定

**理由:** (L2) は LLM を挟んでも受理範囲は L1 と同じ。LLM 呼び出しが純粋な overhead になる。(L3) は受理範囲を広げる代わりに PAuth の zero FP/FN 保証を犠牲にする。
中
**ユーザの回答:** 直接回答なし。直 NL slice ではなく run() コードを挟む理

由を質問 → 説明。その後ゲートウェイ実装のリオーガナイズ + Claude Code 統合の方向へ展開。

---

## Q10. Claude Code の tool 呼び出しを **どこで** 捕まえるか

**質問:** Claude Code を無改造で動かしたまま、外部 tool 呼び出しを gateway で捕捉するメカニズムは?

- (I1) MCP サーバとして自己提示
- (I2) HTTP forward proxy(TLS MITM)
- (I3) ネットワーク namespace + DNS hijack
- (I4) Claude Code を改造する妥協

**私の推奨:** (I1) MCP server を基本にし、(I2) HTTP proxy を補助層に

**理由:**
- (I1) 単独は Bash の `curl` で容易に bypass される
- (I3) は macOS では実装が苦しい、配布性が悪い
- (I4) は要件違反
- (I1)+(I2) で正規ツール経路と抜け道の二段防御
- ただし「Claude Code 自身が積極的に bypass しようとしない」前提を許容する必要がある

**ユーザの回答:** 未回答

---

## Q15. Validator 強化: Semantic judge(LLM as a judge)

**背景:**

A1 の構造を関数として見ると:

```
prompt --[LLM]--> code
            │
            ▼
       [validator]  ← grammar チェックのみ(現状)
            │
       OK ──┴── NG → 反例を返して retry
```

validator は実質「LLM 出力に対するテスト関数」として動作している。現状はテストが文法しかなく、**意味論(prompt の intent を捕捉しているか)を見ていない**。結果、Q14 で議論した「LLM が grammar を満たすために intent を勝手に削る」現象が素通りする(B1 / two_products / post_action ケース)。

ユーザの観察(2026-06-04):

> 「validator はテスト関数として作用している。だからここに色々テスト関数を挟むことでより validator を厳しくしたい。今回はプロンプトの意図を汲み取れていないから FP が起きたのでここに LLM as a judge を追加して意味論チェックを行うようにして」

これは Q14 とは別レイヤの提案:

- **Q14 PreAuth Grill Layer** — PAuth の手前で **ユーザに対話確認** を取る(interactive)
- **Q15 Semantic judge / validator** — agentic A1 の中で **LLM が LLM 出力を判定** し、必要なら deterministic validator も重ねる(non-interactive、自動)

両者は補完的。Q15 は user 介入なしに自動回せるので Phase 1 に乗せやすい。Q14 はユーザに分かりやすいが UX コストが高い。

**2026-06-09 方針更新:** Q15 の目的は「plan が prompt の intent を完璧に模倣していること」を証明することではない。自然言語の曖昧さが根にあるため、完全な intent 同値性の判定は不可能に近い。ここで狙うべきなのは、**prompt と slice / plan の意味論的整合性を片側安全性として検査し、prompt から正当化できない過剰認可(false-positive accept / over-authorization)だけを弾く**こと。

このため validator は、ユーザ要求の一部を落とした「狭すぎる plan」を correctness 問題として扱う一方で、権限境界としては主に以下を拒否対象にする:

- prompt にない side-effecting tool call を plan / slice が許可している
- prompt にない recipient / account / file path / product / amount / channel などの operand を許可している
- prompt にない read→write / read→external-send の data flow を許可している
- prompt の条件・上限・比較・数量詞を外して、より広い実行を許可している

validator が厳しすぎて正しい plan を reject する over-rejection は、security failure ではなく retry loop / clarification で回復する UX・可用性問題として扱う。逆に、過剰認可を accept することは PAuth の前提を壊すため最優先で避ける。

**質問:** Semantic judge を agentic A1 の retry loop にどう組み込むか?

**選択肢:**

- (J1) Grammar 検証の直後に挟む — grammar OK → judge → 両方 OK で成功、judge NG なら反例を retry プロンプトに乗せて再生成
- (J2) Grammar とは独立に外側で挟む — 別ループで grammar が通った final code に対してのみ判定、NG なら全体を最初から retry
- (J3) Judge を retry の最初に挟む — まず prompt と任意の生成 sample で intent を確認、その後 grammar 検証
- (J4) Judge を入れない — Q14 の grill layer に振る

**私の推奨:** **(J1)**

**理由:**

- 既存の grammar feedback loop と同じ messages stream に統合できる(実装単純)
- Grammar 違反より intent 違反のほうが上位の判定。Grammar が通ってない code を判定する意味はない
- (J2) は loop 構造が二重化して messages 履歴が分かれる、無駄
- (J3) は順序が逆。生成→検証の自然な流れを崩す
- (J4) は automated check の機会を捨てる、Q14 との両立は補完関係

**判定プロンプト / validator の要点:**

1. **One-sided safety** — `intent_captured` ではなく「prompt から正当化できない権限が plan / slice に含まれるか」を第一判定にする
2. **Action entailment** — side effecting action が prompt に明示・含意されているか
3. **Resource / operand entailment** — recipient、account、file path、product、amount、channel、数量などが prompt の範囲を超えていないか
4. **Data-flow entailment** — sensitive read の結果を外部送信・書き込みに使う flow が prompt から正当化できるか
5. **Conditions / Quantifiers / Constraints** — "cheapest", "under $80", "if success", "all" 等の条件を外して広い認可にしていないか
6. **Over-rejection tolerance** — 狭すぎる plan や厳しすぎる reject は retry / clarification に回し、過剰認可 accept を最小化する

判定 LLM の出力は当面 JSON: `{"safe_to_authorize": bool, "over_authorization": [str, ...], "missing_or_ambiguous": [str, ...]}`。`safe_to_authorize=false` なら `over_authorization` / `missing_or_ambiguous` を retry プロンプトに乗せて LLM に修正依頼する。将来的には `ACCEPT` / `REJECT_OVER_AUTHORIZATION` / `NEEDS_CLARIFICATION` の tri-state に分ける。

**底打ちの正しい挙動:**

Restricted grammar が **原理的に表現できない** intent(else / nested-if が必須の構造)は、何回 retry しても judge が通さない → `max_retries` 後に reject。これは正しい挙動で、現状の「simplification を受理してしまう FP」を直接潰す。

**コスト:**

- 1 attempt あたり LLM 呼び出しが 1 → 2
- Worst case で 4 attempts × 2 calls = 8 LLM calls / prompt
- 許容範囲(Q14 が入る Phase 2 では更に layered になる)

**残課題:**

- (Q15-a) Judge LLM の確率性 — judge 自身が誤判定する。生成と同じモデルだと相関するので、production では別モデル / アンサンブルが要る
- (Q15-b) Judge の overrejection — 正当に安全な code を「足りない」と誤判定すると retry / clarification が増える。これは許容可能だが UX・コスト指標として測る
- (Q15-c) Judge の prompt injection 脆弱性 — code 中のコメントや変数名に「intent OK」と書かれたら騙されうる。Judge の system prompt で対策必要
- (Q15-d) False-positive accept の定義 — PAuth runtime の FP/FN と混同しないよう、semantic validator では「prompt から正当化できない過剰認可を accept した」事象を最重要 failure として別名で記録する
- (Q15-e) Deterministic validator との分担 — 明示禁止 tool、金額上限、宛先追加、read-only prompt から write tool への拡張など、LLM judge に任せず機械的に弾ける検査をどこまで前段化するか
- (Q15-f) 実装未定 — 方針は one-sided semantic validator + retry loop で固めるが、具体的な prompt、IR、fixture、評価指標は研究課題として残す

**ユーザの回答:** one-sided semantic validator + retry loop の方針を採用。ただし具体実装(prompt、IR、deterministic validator の分担、fixture 設計)は未定で、最も研究が必要な部分として残す。

---

## 未確定の design parameter(まとめ)

| Q | トピック | 状態 |
|---|---|---|
| Q2 | snapshot vs git | γ'(cross-file atomic, agent 専用ブランチ) |
| Q5 | Phase tracking | 未確定(アタッチメント方針で moot) |
| Q6 | Agent → run() ブリッジ | 未確定 → ゲートウェイ案で実装 |
| Q8 | 実装スコープ | 全推奨採用、実装済み |
| Q9 | A1 戦略 | 未確定(L1 で実装済み) |
| Q10 | 捕捉メカニズム | 未回答 |
| Q15 | Validator semantic judge | **J1 採用、agentic A1 に組み込み実装中** |
