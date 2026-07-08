# 開発計画 (plan)

最小MVPから製品へ、段階的に積み上げるチェックリスト。

**使い方:** 完了したら `- [ ]` を `- [x]` にする。各ステージは独立して出荷可能・依存順に並んでいる。

**目印:**
- **🔬研究** — 正解が未確定で、研究・実験・評価が必要な箇所。
- **💬議論** — 設計判断・方針決定が未了で、着手前に議論が必要な箇所。
- 目印なし — 主にエンジニアリング（やり方は概ね決まっている）。

設計詳細は専用文書へ: `architecture.md`（論理設計）, `THREAT_MODEL.md`（防御境界）,
`gateway/INGRESS_DESIGN.md`（ingress）, `gateway/DESIGN_STATUS.md`（現状/未決）,
`solution.md`（実装中の決定 S番号 ＋ 設計対話 Q番号の出典。旧 `grill.md` を統合）。
**課題カタログ（旧 `issues.md`, ID A1〜F1 保持）は本書末尾**。

## ターゲット

- **ToB。** ToC は課金しない想定 → 決済対応は作らない。
- **橋頭堡 = 自社エージェント層（B）。** ingress は **SDK直接統合（Mode 1）**。
- 拡大軸: 任意プロンプト対応 → スイート数 → 対応エージェント（Mode 2）→ 対象層。

---

## Stage 0 — 論文再現

> 主張: 「A1 が文法適合コードを生成できた範囲で benign zero-FP / injection zero-FN」

- [x] 決定的コア: A2 slicing / A3 rules / B1–B4 enforcer / envelope（`pauth/`）
- [x] A1 決定的 recognizer（L1）（`gateway/planning/core.py`）
- [x] A1 LLM freeform ＋ grammar feedback loop（`gateway/planning/agentic_a1.py`）
- [x] shopping suite（自己完結・reference code 同梱）（`pauth/suites/shopping.py`）
- [x] AgentDojo 4 suite アダプタ（banking/slack/travel/workspace）（`tests/experiment/agentdojo_adapter.py`）
- [x] FP/FN 実験ランナー（Table 2 形式）（`eval/fpfn.py`）
- [x] worked example オフライン検証（API不要）（`tests/test_worked_examples.py`）
- [x] **Exit:** A1 が通った全タスクで over-authorization accept = 0 を計測
  （`full_run.json`: FN=0 / 475 injection runs、over-rejection(FP)=7 / 62。
  正式名の集計は `eval/fpfn.py` に追加済み — solution.md S7）

---

## Stage 1 — 製品核 MVP（SDK ＋ credential broker ＋ enforcement）

> 主張: 「乗っ取られた自社エージェントは、承認計画の*構造*を超えるSaaS実行・
> 宛先/金額の改ざんができない」。grill 不要・推論プロキシ不要。

- [x] SDK ingress（Mode 1）: `submit_user_prompt` / `handle_tool_call` の公開API
  （`gateway/runtime/gateway.py`, `gateway/ingress/agent_channel.py`。pip package 化は未了）
- [x] **💬議論** credential broker を採用するか → **採用**（solution.md S4）
- [ ] credential broker 実装: 鍵保持・rotation・隔離（B2）— 最初の実 SaaS と同時に着手
- [x] `architecture.md` §9 を broker モデルに更新（「鍵を見ない」→「鍵を持つ」, S4 で反映済み）
- [x] B1–B4 default-deny ＋ envelope 記録の SDK 経路結線（`Gateway._accept_draft` / `handle_tool_call`）
- [x] **💬議論** 側チャネル（生Bash等）の scope 宣言 → **Stage 1 は禁止前提**（solution.md S6。
  SELF_HOSTING「Egress Lockdown」/ hooks README 4b に明記済み ＋ egress lockdown で機構化）
- [x] self-host 起動手順（`gateway/SELF_HOSTING.md`, `gateway/hooks/README.md`。broker 手順は未了）
- [x] AgentChannel の forwarding 信頼前提を明文化（A4）（`agent_channel.py` docstring）
- [ ] **Exit:** 1つの実SaaS（or 1 MCP）で end-to-end、生Bashなし前提で動作
  — 実ツールは **GitHub** に決定（solution.md S5）、統合は未了

---

## Stage 2 — 任意プロンプト対応（freeform A1 ＋ semantic judge）

> 主張: 「任意プロンプトでも過剰認可は片側安全validatorで弾く。狭すぎる計画は
> retry/clarification で回復」

- [x] LLM A1 freeform を主軸 ingress に昇格 → `auto` 戦略（recognizer fast path ＋
  freeform フォールバック）を既定化（solution.md S2）
- [x] **🔬研究** semantic judge（Q15, 片側安全性, J1）を retry loop に組込み — v1 実装済み
  （`agentic_a1.py`）。prompt・IR・fixture・評価指標の最適化は F1 として研究継続
- [ ] **🔬研究** judge 判定軸（action / operand / data-flow / 条件緩和の entailment）の確立
  — 現 rubric は action/operand/条件まで。data-flow 軸と構造化 IR は未着手
- [x] **💬議論** 機械的検査の前段化範囲（Q15-e）→ 決定・実装済み: 禁止tool・宛先・金額/数量・
  write根拠キーワードの 4 種を `gateway/planning/prechecks.py` で決定的に検査し、
  retry loop 内と `Gateway._accept_draft`（ハードゲート）の 2 点で強制（solution.md S1）
- [ ] **🔬研究** judge を生成と別モデル化／アンサンブルで相関を切る（Q15-a）
  — 別モデル化は済（既定 Anthropic judge ＋ OpenAI fallback, solution.md S3）。アンサンブルは未着手
- [x] over-authorization accept / over-rejection を別名で計測（Q15-d）（solution.md S7）
- [x] **Exit:** AgentDojo freeform で over-authorization accept = 0、over-rejection は計測のみ
  — 達成（2026-07-05, `results/agentic_full.json`）。AgentDojo 4 suite 97 タスク、
  agentic pipeline（gpt-4.1 生成 ＋ Q15-e precheck ＋ gpt-5-mini judge）:
  **over-auth accept = 0 / 156 injection runs**、runtime over-rejection = 0 / 18。
  受理 18 / plan-deny 55 / grammar skip 24（受理率 18.6% — 受理率向上は Stage 4）。
  shopping freeform も over-auth accept = 0（canonical 6 ＋ AI 8）。solution.md S9

---

## Stage 3 — スイート拡張（実ツール源を増やす）

> 主張: 「複数実SaaSで計画外実行を default-deny。危険データフローは検出して deny」

- [x] 多 SuiteSpec 合成（registry）: MCP(HTTP) アダプタ — `merge_suites`＋`HTTPTransport`/`build_mcp_suite` 実装済み（`gateway/providers/registry.py`, `mcp_suite.py`, mock は `tests/fixtures/mock_mcp_server.py`）
- [x] MCP(stdio) アダプタ ＋ ヘルスチェック/再起動 supervisor（B4）— `StdioTransport` に
  `is_alive()`＋自動再起動（crash-loop 上限＋`on_restart` 再初期化フック）。subprocess kill →
  次の rpc で透過的に再spawn（`gateway/providers/mcp_suite.py`, `tests/test_mcp_supervisor.py`）
- [x] OpenAPI 反映アダプタ — `gateway/providers/openapi_suite.py`（spec 反映）＋ `api_spec_monitor.py`（変更検出）実装・テスト済み（`tests/test_openapi_suite.py`）。ホットリロードは未了
- [x] tool名 namespacing（D2）— `merge_suites(namespace=True)` で `<suite>__<tool>`（識別子安全）。既定は collision で raise（現状維持）。runner が namespaced→owner にルーティング（`tests/test_registry_namespace.py`）
- [ ] suite_filter で A1 prompt 膨張抑制（D1）
- [ ] **🔬研究** embedding/LLM ベース suite_filter（キーワードの限界を超える, D1）
- [x] **filter が必要ツールを落とした率**を新指標として計測（盲点対策）— `FILTER_DROP_COUNT` /
  `FILTER_RECALL` を top_k でスイープ（`eval/filter_recall.py`）。ラベル付きコーパス
  （`tests/fixtures/filter_cases.py`）で top_k=1 が workspace を落とす盲点を surface（top_k≥2 で recall 100%）
- [ ] **💬議論** free-operand 3段ポリシー設計: enforced / free / flow-constrained free（E1）
- [x] **💬議論** 危険フロー(#1)の閉じ方を再整理（solution.md S15/S17）→ **別建ての「決定的
  危険フロー検出エンジン」は中核要件ではない**と結論。#1 の closure は **grill 機構**
  （各 sink のオペランド確定地点で人間が実値を確認 → 凍結）。書き込み後の読みも「各
  オペランド地点で必ず grill」で自動的に閉じる。→ 機構は **Stage 5** へ移送
- [ ] **🔬研究** 危険フロー検出（trust ラベル＋テイント）は **正確さの要件でなく、
  grill 選別のための最適化**（fan-out で「全部 grill」が盲判子化するのを防ぐ, 規模対策）。
  静的検出で足りる（制限文法ゆえ）。→ **grill-me UX（S12/S13）＋ 規模が問題化してから**
- [ ] 大きい tool 戻り値の envelope store 圧迫対策（flatten/参照渡し, D3）— **保留（安全上）**: envelope
  verify が改ざん検出のため concrete を毎回再シリアライズする核心機構ゆえ、flatten は tamper 検出を弱める。要 slice 認識設計
- [ ] **Exit:** 2–3個の実MCP/SaaSで動作、filter取りこぼし計測（危険フローの人間確認は Stage 5）

---

## Stage 4 — 文法表現力の拡張（A1.1・研究）

> 主張: 「より広い意図クラスを、決定的コアの保証を保ったまま扱える」

- [x] **💬議論🔬研究** 方式決定 → **プロンプト事前分解 ＋ 有界 fan-out**（Appendix A 無改造、
  guard は `<Condition>` 固定、N は観測から決定・N_max で被害半径を制限。solution.md S10/S11）
- [x] **🔬研究** 拡張が A2/A3 の決定性を壊さないことの検証 — 各 stage は無改造の
  `prepare()` を通過。合成層の 4 性質（不活性・非累積・テンプレート健全・有界権限）を
  敵対的テストで機械検証（`tests/test_composite.py`）。形式的証明ではなくテストによる
  検証である点に注意
- [x] 「成功したら次へ」クラスのタスクが通ることを検証 — B1_cheapest（Stage 2 で reject）が
  参照分解でオフライン end-to-end 通過。fan-out（各要素にアクション）も shopping で通過
- [ ] LLM 分解器（プロンプト → CompositePlan の planner 戦略）＋ 分解忠実性の検証
- [ ] AgentDojo 拒否タスク 79 件の意図クラス分類（逐次依存 / fan-out / 集約 / その他）と救済率計測
- [ ] 集約型ループ（反復横断の状態）の扱い — gather stage 案の設計（🔬研究）
- [ ] **Exit:** Stage 2 で judge が reject していた意図クラスが安全に通る
  — 参照分解では達成。LLM 分解器経由での達成が残り

---

## Stage 5 — grill / HITL（Q14）

> 主張: 「危険フローは粗い注入を人間が止める。精巧な注入は scope外（緩和）」

**設計方針（solution.md S12–S17 で確定）:** #1(汚染データフロー)の closure は
**2フェーズ ＋ grill 機構**。読みスライスを先行実行 → 実値を人間が確認 → 凍結定数で
書き込み。書き込み後の読みも「各 sink のオペランド確定地点で必ず grill」で自動的に
閉じる（「書き込み後」を検出する必要すらない）。agent-drives（無改造 Claude Code）
では **タスク分割でなく「1プラン＋その sink 呼び出しをその場で確認ゲート」**（分割は
認可空白を作るため不可, S15）。確認は側チャネル（モデルコンテキスト外）で行い、agent
へ返す理由文は値ゼロ（S16 実装済み）。

- [ ] **💬議論** grill 実装方式の選択: G1(非対話 reject) / G2(Claude Code の additionalContext で半対話, 推奨) / G3(独立UI)（Q14）
- [x] **#1 closure 本体（実装済み・全経路に一本化, S18/S19）**: 各 sink の制御オペランドを
  確定地点でゲートし、実値を側チャネル（`pending_confirmations`/`confirm`）で人間確認 →
  承認で通す。session/composite 両経路で発火（S19 で一本化、`gateway/runtime/confirmation.py`）。
  taint は静的プロベナンス（laundering 不可, S20）、fail-closed 対応済み（`SourceTrust.fail_closed`）。
  残: (a) fan-out stage の provenance 保存, (b) 側チャネル UI, (c) 2フェーズ自動化,
  (d) grill-me UX 選別（S12/S13）
- [x] 中身/制御オペランド分離ルール（汚染データは content にのみ可・control はゲート, S15/S18）
  ＝ precheck の recipient/amount 分類を再利用して実装。テスト済み（`tests/test_confirmation.py`）
- [x] provenance 表示（「この値は信頼できない外部データ由来」）— `PendingConfirmation.source` に
  出所ツールを付与（静的テイントから導出）。側チャネル確認で「この値は read_email 由来（untrusted）」
  と表示（`eval/grill_scenario.py`, `gateway/runtime/confirmation.py`）
- [ ] **grill-me UX 選別**（例外だけ見せる・出所グループ化・許可リスト昇格, S12/S13）
  — 規模(fan-out)で「全部 grill」が盲判子化するのを防ぐ最適化。正確さの要件ではない
- [ ] **💬議論** 品質grill をバンドル（機構共有・サリエンス分離・起動判定独立）
- [ ] grill 入力ソースをユーザのみに構造的に拘束（不変条件#1 保持）
- [ ] **🔬研究** 疲労対策の信頼スコア（「同一パターン」の判定基準, Q14-a）
- [x] **🔬研究** grill 層への注入対策（grill 自身の threat model, Q14-c）— agent 向け
  フィードバックの構造的無害化を実装（値ゼロ・型強制, S16, `gateway/runtime/feedback.py`）。
  残: grill 表示(人間向け)の threat model
- [ ] **🔬研究** 確認済み prompt 再構成の改ざん対策（feedback loop, Q14-d）
- [x] **Exit:** 危険フローが deny でなく人間確認で通せる（無人時は deny に退避）— 機構＋eval で
  実証（`eval/grill_scenario.py`: 攻撃=REJECT/正当=APPROVE/無人=fail-safe deny、`eval/grill_eval.py`:
  FN=0・VALUE_LEAK=0・APPROVAL_COUNT=理想）。残: 側チャネル確認 UI（B3）

---

## Stage 6 — Mode 2 傍受（無改造エージェント = A経路）

> 前提: B（Mode 1）が実証されてから。コア共有なので ingress を1枚足すだけ。

- [x] 推論プロキシ（capture）＋ ツールプロキシ（enforce）の**ロジック核を実装**
  （`gateway/serving/proxy.py`, S22）: intercept→inspect→forward/block。prompt 捕捉・
  ツール認可・値ゼロ block・側チャネル block をテスト済み（`tests/test_proxy.py`）。
  残: TLS 終端（base URL 差し替え/MITM）と wire 配線（インフラのシェル）
- [x] ツールプロキシ（外部呼び出しの強制点）— 上に同じ（permit=転送/deny=block, S22）
- [ ] **💬議論** 応答書き換えで内蔵ツール（Bash等）を gate するか（B5・脆さ）
- [x] **💬議論** Bash 取り扱い方針 → 既定は **B1 相当（側チャネル全面 default-deny）** を
  機構化（`SideChannelPolicy`, S21）。gateway を通った Bash 等は無条件 deny、allowlist で例外化。
  out-of-band 迂回は保護レベル報告で開示。残: 専用ツール化・応答書換（Stage 6 本体）
- [ ] **💬議論** ファイル系認可方針: R1(git 管理下のみ rollback＋他は別機構) / R2(穴を受容) / R3(read allowlist＋write rollback＋Bash制限)（grill Q3）
- [ ] 同一 `AgentChannel` 契約への正規化
- [x] 保護レベル表示（L0–L3）で実効保護を明示 — `Gateway.protection_report()` 実装（S21）。
  非隔離では out-of-band 迂回を caveat として必ず開示（`tests/test_protection.py`）
- [ ] **💬議論** サブスク認証の扱い（当面非対応と明記するか, TOS/技術の壁）
- [ ] **Exit:** 無改造 Claude Code を Mode 2 で保護、保護レベルを正直表示

---

## 横断的要素（各ステージで並行）

- [x] 正直な計測の維持（FP/FN・over-rejection・over-auth accept）— eval 基盤を `eval/` 直下に
  集約。指標: `FN_COUNT` / `FP_COUNT` / `ACCEPTANCE_RATE` / `OFF_INTENT_COUNT` / `APPROVAL_COUNT` /
  `VALUE_LEAK_COUNT` / `SLICE_GENERATION_FAILURES` / `ADDITIONAL_COST` / `LATENCY`
  （`eval/fpfn.py` / `eval/freeform.py` / `eval/grill_eval.py` / `eval/grill_scenario.py` / `eval/l2_replay.py`）。
  残: filter取りこぼし計測（Stage 3）
- [x] observability / audit（permit/deny ＋ 理由を構造化イベントに）— `AuditEvent`（seq/kind/
  decision/tool/reason_code/reason）を submit の accept/reject と tool_call の permit/deny/pending で
  記録、`gateway.audit_log()` で参照（`gateway/runtime/audit.py`）。値を含む operator 向け
- [x] session 永続化（B1）— `SessionStore`（JSON, atomic write）＋ `restore_channel`（restart 後に
  prompt 再生で plan 再確立）。http_server に opt-in 結線（`--session-store` / `SESSION_STORE_PATH`、既定off）。
  cloud KV backend と署名根分散化は将来（`gateway/serving/session_store.py`）
- [ ] **♾️🔬研究** Judge 機構の継続最適化（モデル/プロンプト/構造/workload, F1 — 永続テーマ）
- [ ] テストデータ整備: AI fixture の人間review（C1）, L3重複解消（C2）, review tooling（C3）
- [ ] AgentDojo を本番依存から外す（import遅延化維持, D4）
- [ ] strict/log モード切替の運用基準を明文化（E3）
- [ ] ファイル系 reversibility / agent-side rollback（cross-file atomic checkpoint,
  grill Q2 → γ' 方向決定済・実装未了）: snapshot か agent 専用ブランチ。主に Mode 2 の
  コーディングエージェント文脈。Bash/ファイル認可の**未決方針は Stage 6** 参照

## 先に決めるべきこと（ブロッカー）

- [x] **💬議論** credential broker を採用するか → **採用**（solution.md S4）
- [x] **💬議論** Stage 1 の最初の実ツール → **GitHub**（solution.md S5）

---

## 課題カタログ（旧 issues.md, ID 保持）

他文書（`architecture.md` / `solution.md` / `DESIGN_STATUS.md`）は ID（A1, B5 等）で
参照しているため ID を保持。状態: 🔴未解決 / 🟡暫定対処 / 🟢主要対策済 / ⚪着手しない /
♾️永続。各項目は対応 Stage を示す。

### A. PAuth の構造的限界

- [ ] 🔴 **A1** grammar 表現力不足: 「比較してどちらか」「成功したら次へ」が Appendix A
  文法に乗らない。選択肢: 緩和/事前分解/別形式。関連 Q9,Q12,Q14。→ **Stage 4（🔬研究）**
- [ ] 🟡 **A2** intent 検証層: 片側安全性 validator v1 実装済み（semantic judge ＋ Q15-e
  決定的 precheck の 2 層, solution.md S1/S3）。judge の prompt/IR/評価指標の最適化は
  F1 として継続。関連 Q14,Q15。→ **Stage 2（🔬研究）**
- [ ] ⚪ **A3** UI プロンプト内 injection は scope 外: ユーザ自身が貼った injection は
  弾けない（論文§3）。関連 Q11,Q14。→ **scope外（`THREAT_MODEL.md` に明記）**
- [ ] 🟡 **A4** agent forwarding 整合性が新信頼前提: AgentChannel で「agent が prompt を
  改変せず転送」前提が新規。構造強制済。関連 Q13。→ **Stage 1**

### B. Gateway 実装の隙間

- [x] 🟢 **B1** session 永続化を実装（`SessionStore`＋`restore_channel`, file-backed）。cloud KV は将来。
- [ ] 🔴 **B2** OAuth/APIキー管理機構が無い: 複数ユーザで credential 隔離/rotation/audit。
  broker **採用は決定済み**（solution.md S4）、実装未着手。→ **Stage 1（credential broker）**
- [ ] 🟡 **B3** Grill UI 未実装: closure 設計は確定（2フェーズ＋sink ゲート＋側チャネル確認,
  solution.md S15/S17）。agent 向けフィードバックの無害化は実装済み（S16）。残: grill 機構
  本体（2フェーズ実行・sink ゲート・確認 UI）。推奨 G2。→ **Stage 5**
- [x] 🟢 **B4** stdio MCP subprocess のヘルスチェック/再起動を実装（`StdioTransport` supervisor）。
  → **Stage 3**
- [x] 🟢 **B5** Bash escape hatch: gateway を通った側チャネル(bash 等)は **default-deny を機構化**
  済み(`SideChannelPolicy`, S21。名前空間付き `suite__bash` も捕捉)。out-of-band 迂回(フック
  未経由の subprocess/直接NW)は **egress ロックダウン(`gateway/deploy/egress_lockdown.sh`, Q10)
  でネットワーク面を防止**(非管理ユーザ前提)。残余: 管理者権限エージェント／非NW副作用の
  FS 隔離。関連 Q4,Q7,Q10。→ **Stage 6（隔離/応答書換）**

### C. テストデータ / fixture

- [ ] 🟡 **C1** AI 生成 fixture の expected_accept が信頼できない。残: 人間 review。
- [ ] 🟡 **C2** L3 reference data 重複（shopping `_TASKS` vs `l3_references`）。選択肢:
  パッケージ化で tools/tasks 分離。
- [ ] 🔴 **C3** fixture review tooling 無し。選択肢: Web/TUI で review サイクル高速化。
  → C1〜C3 まとめて **横断（テストデータ整備）**

### D. 規模拡張で顕在化

- [ ] 🟡 **D1** A1 token 膨張: 登録 MCP 増で prompt 線形膨張。現状 suite_filter。選択肢:
  embedding/LLM filter。→ **Stage 3（🔬研究）**
- [x] 🟢 **D2** multi-suite tool 名衝突: `merge_suites(namespace=True)` で `<suite>__<tool>` に解決（識別子安全な区切り）。
  → **Stage 3（💬議論）**
- [ ] 🟡 **D3** 大きい tool 戻り値が envelope store を圧迫。選択肢: flatten+symbolic化/参照渡し。
  → **Stage 3**
- [ ] 🟡 **D4** AgentDojo は重い optional 依存。現状 import 遅延化、本番未使用。→ **横断**

### E. 仕様の曖昧さ / 規約

- [ ] 🟡 **E1** MCP tool の operand 検証深度が tool 任せ（nested/Union で best-effort）。現状
  `PolicyAwareEnforcer` で free 宣言。残: free 宣言の推奨/自動判定。→ **Stage 3（free-operand）**
- [ ] 🟡 **E2** recognizer の決定的サブセットが狭い（4 正規表現のみ）。`auto` 戦略の
  freeform フォールバックで緩和済み（solution.md S2）。→ **Stage 2**
- [ ] 🟡 **E3** strict/log モード切替の運用ガイド: log 常用で enforcement 縮退。残: strict 切替
  criteria 明文化。→ **横断**

### F. 永続研究テーマ

- [ ] ♾️ **F1** Judge 機構の最適化: モデル/プロンプト/構造/workload に依存する永続的最適化
  テーマ。関連 Q15,Q14,Q12。→ **Stage 2 ＋ 横断（🔬研究・♾️永続）**
</content>
