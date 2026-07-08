# Threat model and defenses

このドキュメントは PAuthGateway が防御する脅威、各脅威を防ぐメカニズム、そして
—— 最も重要な点として —— **どの防御が実際に構築済みで、どれが設計のみか**を列挙
する。`architecture.md` §5（enforcement-core の threat model を列挙）を拡張し、
設計議論で詰めた indirect-prompt-injection / operand-provenance レイヤを加える。

> 本ファイルの正直さルール: 脅威が ✅ **Resolved (built)** とマークされるのは、
> 今日コードがそれを enforce している場合**のみ**。provenance / taint / egress
> レイヤ（§3、§4）は**設計であって実装ではない** —— 参照先のコードが存在する
> までは、稼働中の防御として引用してはならない。

## 0. The conceptual spine (read this first)

2つのフレーミング軸が、別個の脅威を1語に潰してしまう（「injection をブロック」
「free operand 問題」）という反復的な混乱を防ぐ。

**Axis 1 — enforcement gap vs intent-capture gap.**
- *Enforcement gap*: エージェントがユーザー承認済みの計画の外で行動する。これは
  authorization の問題。**決定的で解決済み**。
- *Intent-capture gap*: 計画そのものがユーザーの意図と一致しない。これは
  correctness の問題。**oracle は存在しない**。完成した計画を人間が確認すること
  によってのみ解決される。

**Axis 2 — action injection vs content/data injection.**
- *Action injection*: 汚染データがエージェントに**新規の計画外アクション**を取ら
  せようとする。locked plan + default-deny によって防御される。
- *Content/data injection*: 汚染データが、**すでに許可されたアクションを流れる
  content や値**を操作する。これが難しい残余であり、§3–§6 の主題。

**反復するアンチパターン（暗記せよ）。** 我々が突き当たった行き止まりはすべて
同じ形をしていた: *攻撃者が制御可能な content をセキュリティ入力として使う*こと。
あらゆるシグナルをセキュリティ判断で信頼する前に問え: **攻撃者はこれを書ける
か？** イエスなら、その防御はすでに破れている。§7 を参照。

## 1. Status legend

| Mark | Meaning |
|---|---|
| ✅ | **Resolved (built)** — 今日コードがこれを enforce している |
| 🟡 | **Designed, not implemented** — メカニズムは合意済み、コードはまだない |
| 🔶 | **Open problem** — きれいな解がない。アクティブな設計リスク |
| ⚪ | **Out of scope (accepted)** — 意図的に防御しない |
| ⛔ | **Irreducible floor** — いかなる設計でも除去できない |

## 2. Resolved — enforcement core (built)

これらは今日 `pauth/` + `gateway/` によって enforce されている（`architecture.md`
§4 invariants、§5 threat table を参照）。**Why it holds** 列は、各脅威が実際に
閉じている理由を述べる —— メカニズムの名前だけでなく、そのメカニズムを十分たら
しめている性質を述べる。

| Threat | Mechanism | Why it holds | Status |
|---|---|---|---|
| Agent issues a tool call not in the plan | Default-deny on missing rule (B1) | **Closed-world enforcement.** 許可された集合は、計画からコンパイルされた rules ちょうどそのもの。明示的に許可されていないものはすべて拒否される。一致する rule なしに「allow」へ至る経路はないので、新規の call が隙間をすり抜けることはできない —— rule の不在が拒否*そのもの*である。 | ✅ |
| Agent substitutes a constant operand (IBAN, amount, subject, date) | Operand match against compiled rules (B2/B3) | operand の値は計画時に rule の中で固定される。チェックは call の operand をその固定値と比較する。いかなる差し替えも不一致となり拒否される。エージェントは rule を書かないので、自分自身の permission を広げることはできない。 | ✅ |
| Agent fabricates a derived value it never observed | Symbolic eval against envelope store; agent-reported values ignored (B3) | エージェントではなく gateway がツールを実行し、結果を署名付き envelope として記録する。operand 解決は envelope store **のみ**を読むので、エージェントが捏造した値には裏付けとなる envelope がない → unresolved → 拒否。エージェントが報告した数値は決して判断の入力にならない。 | ✅ |
| Agent skips an observation and uses its symbolic result | Envelope missing → operand unresolved → deny | derived operand が解決されるのは、上流の observation が実際に走った（その envelope が存在する）場合のみ。observation をスキップすると operand は unresolved のまま残り、enforcer は rule を評価できず拒否する。値を「仮定する」方法は存在しない。 | ✅ |
| Agent calls a sensitive tool out of order | Guard requires upstream envelopes; missing → deny | 順序は上流 envelope への guard 依存としてエンコードされる。順序を違えて呼ぶと必要な envelope がまだ存在しないので、guard は false に評価され → 拒否。シーケンスは、エージェントが自分で順序づけるのを信頼するのではなく、data dependency によって enforce される。 | ✅ |
| Agent re-plans mid-session (e.g. on injection) | `AgentChannel` rejects a second `PromptMessage` | 計画はちょうど一度だけ作られ、以後 immutable である（invariant #1）。2つ目の prompt —— injection が新しい計画をインストールするためのベクタ —— は構造的に拒否されるので、lock 後に計画を変異させる API surface は存在しない。 | ✅ |
| **Action injection** via tool result (new off-plan action) | Plan generated from clean prompt before any tool output exists | **Temporal ordering closes it.** 計画は、エージェントがいかなるツール出力を読む*前*に完全に導出され locked される。だから注入された指示はそれに影響を与えられない。注入された「do X」は計画に存在しないツール呼び出しにマップされる → default-deny (B1)。毒は、それが汚染しえた唯一の判断がすでに下された後に到着する。 | ✅ |
| `"ignore previous instructions"` reaches the **planner** | Planner (A1) never reads tool output | **Structural immunity, not resistance.** planner の唯一の入力はクリーンな user prompt であり、email/web/tool 結果を読むコードパス上には決して乗らない。injection テキストはそこに到達できないので、抵抗すべきものが何もない —— そのチャネルが存在しない。 | ✅ |
| `"ignore previous instructions"` hijacks the **executing agent** | Hijacked agent's calls are still gated against the locked plan | **設計はエージェントが injected であると仮定し、エージェントが抵抗することに依存しない。** 完全に説得されたエージェントでも*ツール呼び出しを発する*ことしかできず、あらゆる呼び出しは immutable な計画に対して B1–B4 を通過する。信念はアクションではない: 計画外の呼び出しは、エージェントが何を「望む」よう説得されたかに関わらず拒否される。 | ✅ |
| Plan does not match user intent (intent-capture gap) | grill-me fills a template; the user confirms the completed plan before execution | **意図を判定できる oracle はないが、人間にはできる。** correctness は enforce 可能な性質ではないので、意図を知る唯一の当事者 —— lock 前に具体的な計画を承認するユーザー —— に意図的に委ねられる。システムは意図を検証すると*主張*しない。人間を検証者にする。 | ✅ (by human) |

## 3. Injection-within-plan layer — 大半 implemented（S18–S20）

これは Axis 2 からの残余: *すでに許可された*アクション内部の content/値を汚染
データが操作する。enforcement core はこれを**カバーしない**。防御は provenance taint
+ sink classification + human escalation で、**中核は実装済み**（solution.md S18/S19/S20）。
残るのは Q-LLM と confirmation の HTTP wire 露出のみ。

Defense components (現状):

| Component | What it does | 実装 |
|---|---|---|
| Source trust label | どのツールが untrusted データを返すかを宣言、**fail-closed で default untrusted 可** | 🟢 `gateway/runtime/confirmation.py`（`SourceTrust` / `SourceTrust.fail_closed`） |
| Taint propagation | 制限文法（単一代入・ループ無し）から、どの制御 operand が untrusted 由来かを**静的 provenance** で追う。変換（`amount*2`）を経ても taint が落ちない | 🟢 `gateway/runtime/confirmation.py`（`static_taint_map`、S20。設計時の runtime "meet" ではなく静的解析） |
| Sink classification | 制御 operand（recipient/amount）を判定。content operand は gate しない（S15 の content/control 分離） | 🟢 `gateway/planning/prechecks.py`（`_classify_param`）+ `confirmation.py`（`control_operands`） |
| Gate B5 | `untrusted × 制御 operand` → **human confirm へ保留**（PENDING_CONFIRMATION）。session/composite 両経路で発火（S19） | 🟢 `gateway/runtime/gateway.py`（`_confirmation_gate`） |
| Confirmation round-trip | 保留値 + provenance を人間の側チャネルへ、承認で解除 | 🟡 Python API 実装済み（`Gateway.pending_confirmations()` / `confirm()`）。HTTP wire（`confirm_request` / `confirm_response`）は未露出 |
| Quarantine LLM (Q-LLM) | untrusted content を**ツールアクセスなしで**読む。出力は untrusted タグ付け | 🔴 未実装 |

注: envelope はすでに**どのツールが各値を生成したか**を記録している（偽造不能、
HMAC 署名済み）。trust label はその origin の*ポリシー解釈*であり、envelope では
なく policy/config に存在する。だからこのレイヤは既存の基盤の上に lookup +
propagation + gate を追加するだけで、署名済みの envelope schema は変更しない。

Threats this addresses（confirmation gate の実装で **mitigated**、既知の残余あり）:

| Threat | Mechanism | Status |
|---|---|---|
| 汚染された source からの derived operand が**制御** operand（recipient/amount）へ流れる（例: メール内の攻撃者制御の IBAN） | Source untrusted → 静的 provenance taint → 制御 operand で PENDING_CONFIRMATION → human confirm | 🟢（S18–S20。残余: fan-out stage は観測定数畳み込みで provenance が落ち under-gate しうる、S20） |
| Free-operand **content** poisoning（例: 「本文に secret を含めよ」）が external sink へ | content operand は gate しない（S15）: 汚染は確認済みの宛先へ届くのみで被害有界。制御へ流れれば上段で保留 | 🟢（content/control 分離） |
| Q-LLM output manipulation（「output IBAN = attacker's」） | untrusted content をツールアクセス無しで読む Q-LLM 想定 | 🔴 Q-LLM 未実装 |

## 4. Open problems — no clean solution yet

| Problem | Why it is hard | Status |
|---|---|---|
| **Egress sink enumeration completeness** | 有害な sink（例: `delete`）が `internal-read` と誤分類されると、injection within plan が黙って gate を通過する。防御は sink inventory の精度までしか良くならない。**これが現在の最弱点。** | 🔶 |
| Per-record trust granularity | Suite-level trust は安全だが粗い（Gmail を全部 untrusted にする → メール由来のあらゆる値が confirm を要する）。より細かい trust は**verified** メタデータ（DKIM/SPF 検証済み送信者、OS path）を使わねばならず、content が主張するメタデータを決して使ってはならない。実際の verification インフラが必要。 | 🔶 |
| Trust configuration error | デプロイ者が untrusted な source を trusted と誤マークすると gate が開く。default-untrusted で緩和されるが、config は今や attack surface である。 | 🔶 |
| Usability cost of egress confirmation | あらゆる `untrusted × egress` アクションがユーザーを中断させる。high-automation のユースケースはこれを痛感する。ここで安全かつ無監督の自動経路は存在しない。 | 🔶 |

## 5. Out of scope (accepted, not defended)

| Threat | Why accepted |
|---|---|
| Injection in the user's own prompt window | input window は trusted —— ユーザーの責任（`architecture.md` §5, Q11） | ⚪ |
| User disables / bypasses the hook | ユーザーは trusted（Q0） | ⚪ |
| Side channels: filesystem, bash, child processes, env vars | gateway はツール呼び出ししか見えない。実際の Claude Code firewall はさらに Bash policy / sandbox を要する（Q7/Q10） | ⚪ |
| Availability attack: a hijacked agent sabotages or derails the task | **有害なアクションを生まない**（計画外の呼び出しは拒否される）—— 進捗の欠如のみ。liveness の問題であって security breach ではない。 | ⚪ |
| Prompt-correctness beyond what the user approved | PAuth は計画を enforce する。correctness oracle ではない。ユーザーは間違ったことをする計画を承認しうる。 | ⚪ |

## 6. The irreducible floor ⛔

上記すべての防御の後、ひとつの脅威が残り、**いかなる設計でも除去できない**:

> **人間が egress confirmation で social engineering され**、明瞭に提示された
> 悪意あるアクションを承認してしまう。

システムの仕事は、問題をこの floor *まで*縮約することだ: confirmation を
**最大限に informed** にし（解決済みの値 + 偽造不能な provenance chain を表示
—— 「IBAN …、`unknown@external` からのメール由来、DKIM 未検証」）、**最小限の
頻度**にする（`untrusted × egress` のみ、plan-time pinning が集合をさらに縮小）。
この floor を除去できると主張する者は誰でも、存在しない correctness oracle を
売りつけている。

## 7. Explicitly rejected anti-patterns

設計で歩いた行き止まり。各々が *攻撃者が制御可能な content をセキュリティ入力と
して使う*（§0）の具体例。再提案されないよう文書化する。

| Rejected idea | Why it breaks |
|---|---|
| An LLM judges whether an operand "aligns with intent" | judge は汚染データを読む → 自身が injectable。「正しい」値についての独立した ground truth を持たない。injectable な LLM を別の injectable な LLM で直すのは turtles-all-the-way-down |
| Decide a source's trust by **reading its content** | trust の判断を、content を書く攻撃者に手渡す（「このメールは信頼できる銀行から」） |
| Trust content-claimed metadata (e.g. the `From:` header) | 偽造可能。暗号的に**検証された** provenance（DKIM/SPF）のみが数える |
| Treat "free operand" as the unit of defense | derived-operand-from-poisoned-source のケースを見落とす（IBAN は*チェック済み*の operand だが、それでもその source で汚染されている） |
| Mark a source trusted so the LLM can "understand" it | カテゴリ錯誤: untrusted ≠ unreadable。読むことは internal で常に許可される。trust は egress のみを支配する。読むために trusted とマークすることは、label が存在して塞ぐべきまさにその穴を開ける |

## 8. End-to-end defense flow

```
User prompt
   │  grill-me fills template; operands classified pinned (USER) vs derived (read-time)
   ▼
HUMAN CONFIRM the completed plan   ← intent-capture gap closed here (§2)
   │  plan locked; never re-planned
   ▼
─── execution; per tool call ───
   ▼
B1–B4  plan enforcement (built, §2)         ← action injection blocked here
   │  permitted by plan
   ▼
B5  taint × sink  (designed, §3)            ← content/data injection handled here
   │   trusted?            → pass
   │   untrusted × internal → pass (no egress harm; reading/understanding never blocked)
   │   untrusted × egress   → CONFIRM
   ▼
HUMAN CONFIRM the egress value + provenance chain   ← irreducible floor (§6)
   │
   ▼
suite.runner executes · envelope records signed observation (built, §2)
```

Untrusted content は自由に読まれ/理解される（ツールアクセスのない quarantine
LLM を含む）。システム全体が最終的に gate するのは唯一**untrusted-sourced な値が
egress boundary を越えること**であり、その判断のために最終的に信頼するのは唯一
人間 + 暗号的に検証可能な provenance である。

## 9. Relationship to other docs

- `architecture.md` §4–§5 — 構築済みの enforcement core とその invariants。
- `design-status.md` — 実装ステータス / bottlenecks。
- `gateway/runtime/policy.py` — 今日 free operands をマークする。§3 はこれを sink
  classification と trust labels で拡張する。
