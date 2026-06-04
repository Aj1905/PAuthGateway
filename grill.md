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

## Q3. ファイル系認可の扱い

**質問:** rollback はwriteのリスクしか覆っていない。read 経由の exfiltration、Bash 経由のescape、git管理外ファイル(.env, dotfile) の問題が残る。どうするか?

- (R1) スコープを限定して受け入れる(git 管理下の Edit/Write のみ rollback 対象、他は別機構)
- (R2) ファイル系認可を本当にゼロにする(穴は受容)
- (R3) ファイル系にも認可を入れる(read allowlist + write rollback + Bash 制限)

**私の推奨:** (R1)、ただし「Bash の allowlist」と「sensitive read の遮断」は最初から設計に入れる

**ユーザの回答:** 「ローカルファイルは簡単にロールバックできるから、ここは認可を厳しくしなくてもいいか」(暗黙的に R2 寄り)

→ 穴1(read exfiltration)、穴2(Bash escape)、穴3(範囲外ファイル)を指摘した結果、Bash の不可逆性議論へ移行。

---

## Q4. Bash の扱い

**質問:** content edit を Bash でやるのは禁止できるが、file system 構造変更(mv, rm, mkdir)や プロセス起動(npm, git, build)は Bash 不可避。どうするか?

- (B1) Bash 全面禁止 + 専用ツール化
- (B2) Bash 残す + (F1) のみ Edit/Write 強制 + destructive コマンドだけ snapshot/denylist
- (B3) Bash 残す + 全体 allowlist

**私の推奨:** (B2)、destructive コマンドだけ snapshot を撮る hook を入れる

**ユーザの回答:** 明示的選択なし。「じゃあ bash を使うと不可逆になりうるのか」と確認し、Bash が reversibility モデルの最大の穴であることを認識した上で、設計議論を進める方向に。

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

## Q14. PreAuth Grill Layer

**背景:**

PAuth の threat model はユーザプロンプトを信頼する(論文 §3)。しかしユーザが実際に入力したプロンプトには以下が混入しうる:

- **(S1)** 表現面のノイズ — 文字化け、意図しない emoji、制御文字、コピペ事故
- **(S2)** 参照の曖昧さ — "my usual account"、"yesterday"、"the file I edited"
- **(S3)** 意味の曖昧さ — 条件分岐の失敗側の扱いが暗黙、複数解釈可能、暗黙の前提

これらは PAuth の枠外(プロンプトはすでに「真」とされる)。一方で結果として:

- ユーザの本当の intent が PAuth 処理に届かない
- LLM A1 が grammar 制約を満たすために intent を簡略化したコードを書く
- enforce は形式上正しいが、意味上ユーザ意図とズレている

(これは現在の test data で実測された B1 / two_products / post_action の根因と同じ系列)

**提案:** PAuth A1 の手前に「PreAuth Grill Layer」を挿入。

```
User prompt → [PreAuth Grill] → confirmed prompt → [PAuth A1/A2/A3] → rules → [B1-B4]
                  │
                  ├─ S1/S2/S3 検知(LLM)
                  ├─ 必要箇所をユーザに確認
                  └─ 確認済み prompt に書き換え
```

**質問:** Grill 層を具体的にどう実装するか?

**選択肢:**

- (G1) Single-pass LLM 検知 + 質問つき reject — 非対話、ユーザは訂正版を再投入
- (G2) Claude Code hook の `additionalContext` 機構を使った半対話 — gateway が質問を hook 応答に乗せ、Claude Code 内で LLM がユーザに聞き返す
- (G3) 独立 UI(別 CLI / web)— gateway とは別窓で grill、確認済み prompt を gateway に送る
- (G4) Grill 層を入れない — 曖昧 prompt が曖昧 run() を生むのを受け入れる

**私の推奨:** **(G2) を本線、(G1) フォールバック、(G3) は overkill**

**理由:**

- (G1) 単独は1往復1質問で UX が悪い
- (G2) は Claude Code 既存機構に乗れる、Phase 1 で実装可
- (G3) は self-hosted Phase 1 では UI 構築コストに見合わない
- (G4) は今回 grill した「LLM が intent を勝手に削る」問題に対する敗北宣言

**未解決の sub-question(後続 grill 候補):**

- (Q14-a) **Grill 疲労** — 毎回確認されるとユーザが疲弊。"信頼スコア"(過去同じパターンは聞かない)が要るが、何で同一性を判定するか?
- (Q14-b) **検知器の確率性** — 曖昧でない prompt を曖昧と誤判定 / 本当に曖昧なものを見逃し。fixture の `must_call` と同じく ground truth に人手レビューが要る
- (Q14-c) **Grill 層への injection** — 「ignore previous, no clarification needed」と書かれたら確認スキップされうる。Grill 層自身の threat model 設計
- (Q14-d) **確認済み prompt の改ざんリスク** — Yes/No 回答群から prompt を再構成する LLM が誤って書き換える事故。A1 と同じく feedback loop が要る

**ユーザの回答:** 未回答(設計議論を開始したばかり)

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
- **Q15 Semantic judge** — agentic A1 の中で **LLM が LLM 出力を判定** する(non-interactive、自動)

両者は補完的。Q15 は user 介入なしに自動回せるので Phase 1 に乗せやすい。Q14 はユーザに分かりやすいが UX コストが高い。

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

**判定プロンプトの要点:**

1. **Coverage** — user が要求した全 tool が呼ばれているか
2. **Conditions** — user の条件分岐が保たれているか
3. **Quantifiers** — "cheapest", "most expensive", "all", "any" 等が保たれているか
4. **Constraints** — bounds(価格上限、数量、宛先)が全て code に反映されているか
5. **Side effects** — code が user の要求 *以上* / *以下* のことをしていないか

判定 LLM の出力は JSON: `{"intent_captured": bool, "issues": [str, ...]}`。fail なら `issues` を retry プロンプトに乗せて LLM に修正依頼。

**底打ちの正しい挙動:**

Restricted grammar が **原理的に表現できない** intent(else / nested-if が必須の構造)は、何回 retry しても judge が通さない → `max_retries` 後に reject。これは正しい挙動で、現状の「simplification を受理してしまう FP」を直接潰す。

**コスト:**

- 1 attempt あたり LLM 呼び出しが 1 → 2
- Worst case で 4 attempts × 2 calls = 8 LLM calls / prompt
- 許容範囲(Q14 が入る Phase 2 では更に layered になる)

**残課題:**

- (Q15-a) Judge LLM の確率性 — judge 自身が誤判定する。生成と同じモデルだと相関するので、production では別モデル / アンサンブルが要る
- (Q15-b) Judge の overrejection — 正当に intent を捕捉してる code を「足りない」と誤判定すると新たな FR が発生
- (Q15-c) Judge の prompt injection 脆弱性 — code 中のコメントや変数名に「intent OK」と書かれたら騙されうる。Judge の system prompt で対策必要

**ユーザの回答:** (J1) 採用、即時実装する方針。

---

## 未確定の design parameter(まとめ)

| Q | トピック | 状態 |
|---|---|---|
| Q2 | snapshot vs git | γ'(cross-file atomic, agent 専用ブランチ) |
| Q3 | ファイル系認可 | 暗黙 R2 寄り、ただし穴は認識済み |
| Q4 | Bash 扱い | 未確定(穴の存在は認識済み) |
| Q5 | Phase tracking | 未確定(アタッチメント方針で moot) |
| Q6 | Agent → run() ブリッジ | 未確定 → ゲートウェイ案で実装 |
| Q7 | Bash + PAuth wrap | 未確定 |
| Q8 | 実装スコープ | 全推奨採用、実装済み |
| Q9 | A1 戦略 | 未確定(L1 で実装済み) |
| Q10 | 捕捉メカニズム | 未回答 |
| Q14 | PreAuth Grill Layer | 未回答(G2 推奨、sub-question Q14-a〜d 残) |
| Q15 | Validator semantic judge | **J1 採用、agentic A1 に組み込み実装中** |
