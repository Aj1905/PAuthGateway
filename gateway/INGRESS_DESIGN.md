# Ingress 設計: まず SDK 直結、interception は後で

このメモは gateway が agent にどう接続するか、そして **SDK / 直接統合を最初の
ingress（beachhead）** とし、**interception ingress（proxy / hooks）は同じ
contract の背後の slot として**後から構築する、という決定を記録する。

本メモは `DESIGN_STATUS.md` の規律に従う。確定した決定と未解決の問いを分離し、
設計が実態以上に固まって見えないようにする。

相互参照: `architecture.md` §1.1/§1.2（ingress 境界 — *adapter* レベルで
"ingress" を使う。ここの directional model を指し示す "Terminology note" を参照。
leg モデルが architecture.md に載るのは interception 実装後だけ）、`plan.md`
issue B5（Bash escape hatch）、`DESIGN_STATUS.md` bottleneck #2（prompt capture が
主要なプロダクトリスク）、`BUSINESS_STRATEGY.md` §3.1（ターゲットセグメントの決定）。

## 中核原則: ingress モードは agent を誰が所有するかで決まる

| agent を所有するのは誰か | Ingress モード | 理由 |
|---|---|---|
| 顧客が構築する（自社開発 agent） | **SDK / 直接統合** | 顧客がコードを所有するので、gateway（pauth core）を直接呼ぶ。interception は不要。 |
| サードパーティ製（無改変の Claude Code, Codex, ...） | **Interception**（inference proxy / hooks） | コードを変更できないので、prompt と tool イベントを外側から捕捉する必要がある。 |

どちらの ingress モードも **同一の** `PromptMessage` /
`ToolCallMessage` contract（`gateway/ingress/agent_channel.py`）に正規化され、
**同一の**決定的コア（`pauth/`）に流れ込む。異なるのは ingress adapter だけ。
これはまさに `architecture.md` の疎結合境界が想定していた用途だ。

ただし注意: **本メモでは "ingress" を2つのレベルで使う** — *adapter*（上記の
SDK vs interception）と、各 capture/enforcement タップの *ワイヤレベルの方向* だ。
capture と enforcement は往復のうち **同じ leg には乗らない**。
Mode 2 を読む前に下記の "Directional model" を参照すること。

## Directional model: "ingress" ≠ 単一方向ではない（往路/復路 × ingress/egress）

agent↔provider のやり取りは **往復（round trip）** なので、gateway から見れば leg は
1つではなく4つある。これらを混同すると、gateway がある leg では *observe* しか
できず、別の leg でしか *enforce* できない、という事実が隠れてしまう。

```text
          往路ingress              往路egress
agent ──────────────────▶ gateway ──────────────────▶ provider
      ◀──────────────────         ◀──────────────────
          復路egress              復路ingress
```

| Leg | ワイヤ方向 | 流れるもの | Gateway の役割 |
|---|---|---|---|
| **往路ingress** | agent → gateway | user prompt（リクエスト入） | prompt を **observe** → `PromptMessage`; plan-once (A1–A3) |
| **往路egress** | gateway → provider | user prompt（リクエスト出） | 中継。送出前に任意で prompt redaction |
| **復路ingress** | provider → gateway | model の `tool_use`（レスポンス入） | tool call を **observe** → `ToolCallMessage` |
| **復路egress** | gateway → agent | response（レスポンス出） | **enforce** — agent が見る前に、拒否された `tool_use` を rewrite/block する (B1–B4) |

ここから2つの帰結が直ちに導かれる:

1. **observation は ingress leg 群に、enforcement は 復路egress に存在する。**
   prompt の捕捉（往路ingress）と tool call の捕捉（復路ingress）は read-only の
   タップだ。tool call を実際に *止める* には **復路egress** — read-write の
   タップ — で動く必要がある。これは「capture is not enforcement」（下記 Mode 2）
   のワイヤレベルでの言明だ。
2. **2つの contract は2つの ingress leg に対応する。** `PromptMessage` = 往路ingress、
   `ToolCallMessage` = 復路ingress。コアは egress に直接触れることはなく、
   **復路egress** leg が適用する decision を返すだけだ。

**tool-execution channel**（agent ↔ MCP / 外部 tool）は、独自の4つの leg を持つ
*2本目の* 往復だ。tool proxy（下記 B）は inference の往復ではなく、**その 往路**
（agent → tool リクエスト）で動く。

各モードがこれらの leg をどう占有するか:

| | 往路ingress | 往路egress | 復路ingress | 復路egress |
|---|---|---|---|---|
| **Mode 1 SDK** | `submit_user_prompt`（帯域外呼び出し） | —（agent が自分で provider を呼ぶ） | `handle_tool_call`（帯域外呼び出し） | decision = 関数の戻り値; **agent 自身のコードがそれを適用する** |
| **Mode 2 inference proxy** | proxy がリクエストを読む | proxy が中継する | proxy がレスポンスを読む | proxy が rewrite/block する — path (A) |

**Mode 1 では gateway は inline ではない**。agent の傍らにいる呼び出し先（callee）で
あり、よって 往路egress は gateway にとって存在せず、「enforcement」は顧客のコードが
従うことに同意する boolean に過ぎない。**Mode 2 では gateway は inline** なので、
4つの leg すべてが実在し、**(壊れやすい) response rewriting が起きざるをえないのは
復路egress** だ — これこそが agent の状態を desync させうる理由そのものだ。

## 決定

- **Beachhead = Mode 1（SDK / 直接）、自社構築 agent / ToB セグメント。** 今すぐ
  構築する。
- **Mode 2（interception）は後回しにする。** ingress 境界を開いたままにして同じ
  contract の背後に接続できるようにするが、**interception adapter はまだ実装しない。**
- **ToC は課金セグメントではない。** 消費者向けの subscription ベースの支払い /
  課金経路は設けない。`BUSINESS_STRATEGY.md` を参照。

構築の規律（3層、これらを潰さないこと）:

| 層 | 今すぐ構築? | 備考 |
|---|---|---|
| 共有コア（`pauth/`、enforcer、envelope、`AgentChannel` contract） | **Yes** | 両モードに供する。基盤だ。 |
| Ingress 境界（クリーンで contract が安定した継ぎ目） | **既に存在** | Mode 2 を後から接続できるようクリーンに保つ。 |
| Mode 1 SDK ingress | **Yes** | beachhead。最初の顧客はこれを使う。 |
| Mode 2 interception ingress（proxy / hooks） | **Partial — core 実装済み** | hooks（`gateway/hooks/`）は稼働。proxy の enforcement core（`gateway/serving/proxy.py` の `InterceptingProxy`、S22）＋ egress lockdown（`gateway/deploy/egress_lockdown.sh`）も実装済み。残るは TLS 終端 / ネットワーク配線のシェルのみ。 |

「両方を build する」とは、両方を *準備する*（共有コア + 開いた境界）という意味で
あって、両方を *実装する* ことではない。Mode 1 が検証される前に Mode 2 adapter を
書くのは、未検証の2つ目のユースケースに対する早すぎる抽象化だ。

---

## Mode 1 — SDK / 直接統合（beachhead、今すぐ構築）

顧客自身の agent コードが gateway を直接呼ぶ。clean な prompt を一度 submit し、
その後は各 tool call を実行前に enforcer 経由でルーティングする。

```text
customer agent code
   ├─ submit_user_prompt(prompt)        → plan once   (pauth A1→A2→A3)
   └─ on each tool call:
        handle_tool_call(tool, args)     → enforce     (pauth B1–B4)
        → allowed → execute → record envelope (B4)
        → denied  → refuse
```

なぜこれが（単なる選択肢ではなく）beachhead なのか:

1. **最も難しい未解決問題を取り除く。** `DESIGN_STATUS.md` bottleneck #2
   （無改変の agent から clean な prompt を堅牢に捕捉する）は **ここには存在しない**
   — 顧客が clean な prompt と tool call を SDK に直接手渡すからだ。base-URL の
   MITM も、hook 除去も、TLS pinning も、TOS のグレーゾーンもない。
2. **provider が制御する面という戦略的リスクを取り除く。** 統合点は provider の
   hook 面ではなく顧客自身のコードだ。provider のインセンティブによってこれが
   劣化させられることはない。
3. **証明可能な L3 プロダクトを今すぐ出荷できる。** 完全な capture + 完全な
   enforcement を、壊れやすさなしに、interception 技術の成熟を待たずに実現する。

市場の実態（美化しないこと）:

- **より狭いセグメント。** ほとんどの企業は既製の agent を使う。自社で agent を
  構築し *かつ* サードパーティの認可フレームワークを望む層はより小さい。だが、
  より洗練され、価値が高く、いったん統合されると粘着性が高い。
  `BUSINESS_STRATEGY.md` の「狭く防御可能なウェッジ」に合致する。
- **より激しい競争。** 「自社 agent を保護する」領域は、無改変 agent firewall 領域
  よりも直接競合が多い（NeMo Guardrails, Guardrails AI, Llama Guard, agent
  フレームワーク群）。差別化は、その場しのぎ / 確率的なチェックに対して
  **決定的で証明可能な task-scoping** に強く依拠せねばならない。
- **framework vs DIY の緊張。** 自社で agent を構築できるチームは、自前のチェックも
  手で書ける。PAuth は自作よりも明確に優れていなければならない。すなわち、原則に
  基づき、envelope に裏打ちされ、plan-once な認可フレームワークであり、正直な
  FP/FN の数値で証明されたものだ。

---

## Mode 2 — Interception（無改変 agent; 後回し、slot のみ）

コードを変更できない agent（Claude Code, Codex）向け。**まだ構築していない。**
境界が後付けではなく設計済みであり続けるよう、ここに記録しておく。

interception のサブモードは agent の認証方式に依存する:

| Agent の認証 | Interception | 備考 |
|---|---|---|
| API キー / API（Bedrock / Vertex / Azure） | **Inference proxy**（base-URL リダイレクト、provider へ中継） | MITM がクリーン。キーは顧客のもの。API 規約は API 上での構築を許容する。ToB に自然に適合。 |
| Subscription（OAuth、席単位） | **Hooks**（`UserPromptSubmit` + `PreToolUse`） | Inference proxy は塞がれている。first-party に紐づくトークン、TLS pinning の可能性、TOS リスク。Hooks は agent ランタイム内で動き、認証方式に依存しない。 |

Subscription の壁（なぜそこで inference proxy が成立しないか）:

1. OAuth トークンは provider の first-party 利用のために発行される。サードパーティの
   proxy 経由で中継するのは、TOS / 「意図しない利用」違反になる公算が高い。
2. TLS pinning がある場合、ローカルネットワークの MITM は無効化される。（現行の
   Claude Code については未検証 — 実機テストが必要。）
3. 信頼を売るプロダクトが TOS 違反の MITM を出荷してはならない。明示的に
   「subscription は非対応。API / Team / Enterprise のみ」とするのが望ましい。

Capture is not enforcement（inference-proxy 経路に適用される） — これは 往路/復路 の
分割を具体化したものだ:

- inference proxy は model が **復路ingress** で発する tool call を **observe** する。
  それ自体は tool call を **block** しない。block するには **復路egress** で動く
  必要がある。
- **(A) Response rewriting**（inference channel の **復路egress** で動く） — model の
  レスポンス中の拒否された `tool_use` を、agent に届く前に rewrite する。agent から
  外に出ない agent 内部の tool（Claude Code の `Bash`、ファイル操作）を gate できる
  — B5 escape hatch に触れる唯一の無改変手段だ。壊れやすい。レスポンスを途中で
  rewrite すると agent の状態を desync させうる。
- **(B) Tool proxy**（inference の往復ではなく、**tool-execution channel の 往路** で
  動く） — MCP / 外部 tool call を gateway 経由でルーティングし、そこで deny する
  （`gateway/providers/mcp_suite.py`）。堅牢だが、agent 内部の tool はここを通らない。
- 完全な L3 interception = (A) + (B) — これらは **異なる channel 上の異なる leg** を
  カバーするので、どちらか一方だけでは完全にならない。

中継が実現可能（新規ではない）ことを示す先行例: LiteLLM, Cloudflare AI Gateway,
Helicone, OpenRouter。新規なのは、その中継に PAuth を載せる部分だ。

---

## 未解決の問い（未決定）

1. **Mode 1 SDK の形。** SDK の表面（surface）はどうあるべきか。最小構成: 既存の
   `Gateway` クラスをラップする `submit_user_prompt` + `handle_tool_call`。言語
   バインディング（まず Python; 他は後で?）。sync vs async。エラー/deny の戻り
   contract。
2. **Mode 1 の差別化の証明。** task-scoping において PAuth が自作チェックや確率的
   guardrail に勝ることを示す、具体的なデモ + 正直なベンチマーク。これは単なる
   コードではなく GTM 上の最重要成果物だ。
3. **Bash / 内部 tool の scope（Mode 2）。** (A) response rewriting 経由でしか到達
   できない。未解決の B5 / bottleneck #5 の決定と交差する。Mode 2 と一緒に先送りする。
4. **Subscription サポートの方針。** おそらく「非対応。API / Team / Enterprise のみ」。
   Mode 2 の作業に着手する前に確定する。
5. **Custody（保管責任）。** plaintext の prompt + キーを見る interception 経路は
   すべて、信頼が確立されるまで **self-host のみ** とする（`BUSINESS_OPERATIONS.md`）。

## Sequencing（順序付け）

1. 共有コアを構築し、ingress 境界をクリーンに保つ（大部分は既存）。
2. **Mode 1 SDK ingress** と差別化のデモ/ベンチマークを構築する。
3. 最初の自社構築 agent（ToB）顧客を Mode 1 に着地させる。
4. **Mode 1 が検証された後にのみ:** Mode 2 interception を実装する。inference-proxy
   + tool-proxy（API/ToB）経路から始める。明確で TOS クリーンな仕組みが存在しない
   限り、subscription は対象外として扱う。
5. 各 adapter がコードに存在してからのみ、`architecture.md` §1.1/§1.2 を実際の
   ingress に合わせて更新する。
