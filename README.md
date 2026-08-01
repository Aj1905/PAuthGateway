# PAuthGateway

**AI エージェントと、それが呼び出す実際のツール・SaaS の間に置かれる、タスク範囲限定の認可ファイアウォール。** ユーザーの汚染されていないプロンプトから計画をちょうど一度だけ導出し、以後すべてのツール呼び出しをその計画と照合する — デフォルト拒否。エージェントがプロンプトインジェクションや汚染されたツール出力に乗っ取られても、コンパイル済み計画の外にある呼び出しは拒否される。その計画がユーザーの依頼を正確に捉えているかどうかは、別途測定される。

*"PAuth — Precise Task-Scoped Authorization For Agents"*(Sharma, Jiang,
Lin & Chen, arXiv:2603.17170)に基づく。最初の統合対象は Claude Code。

- **セキュリティ上の論拠と数値を知りたい** → [`docs/EVALUATION.md`](docs/EVALUATION.md)
- **エージェントの前段でゲートウェイを動かしたい** → [デプロイ](#デプロイ)
- **実験を再現したい** → [再現](#再現)

---

## なぜ必要か

OAuth トークンはスコープ全体への*常設の*アクセス(「メールを送れる」「送金できる」)を付与する。エージェントが乗っ取られれば、トークンが許すすべてを攻撃者も実行できる。トークンが答えるのは「*誰が*この API を使ってよいか」であって、「*この特定の呼び出しはユーザーの依頼の一部か*」では決してない。

PAuthGateway は執行点をエージェントの**外側**に移す:

- **エージェントは無改変** — フックで介入する(将来的には MCP / プロキシ)。
- **計画は一度、執行はすべての呼び出しで** — エージェントは汚染された出力を見た後に計画を書き換えられない。
- **判定は決定的** — 許可の判断を LLM は行わない。LLM を使うのは計画生成だけで、スライス導出、ルールコンパイル、執行は決定的である。
- **観測はゲートウェイが所有する** — 各ツール結果は署名付き envelope として記録されるため、偽造された値が後段の検査を誘導できない。

設計: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) ·
脅威モデル: [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) ·
用語: [`docs/GLOSSARY.md`](docs/GLOSSARY.md)

## これは何で*ない*か

- **正しさの保証ではない。** ユーザーが誤った計画を承認すれば、その計画の*範囲内で*誤ったことが起きる。PAuth が保証するのは「依頼された範囲を超えない」ことだけである。
- **エージェントのサンドボックスではない。** ゲートウェイが見るのはツール呼び出しである。サイドチャネル(Bash、ファイル操作)には別の仕組みが必要。

対象外とする脅威の完全な一覧は [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) の「範囲外(受容済み・防御しない)」節を参照。

---

## パイプライン

```
prompt ─▶ Planner ─▶ Slicer ─▶ Rule compiler ─▶ Enforcer
        (LLM: the      (deterministic)          (default-deny; matches each call's
         only non-                               control operands against the rules;
         deterministic                          signs results into tamper-evident
         step)                                   envelopes)
```

**Planner** が読むのは信頼できるプロンプトとツールスキーマだけであり、信頼できない実行時データは決して読まない。したがって制御オペランド(宛先、金額)の来歴は汚染されていない。**Enforcer** は、ルールが署名付き envelope からその制御オペランドを再導出できる場合に限り呼び出しを認可する。この執行境界は計画相対である: Planner がどう誤ろうと、Enforcer が認可するのは信頼された計画が再導出するものだけである。その計画自体がユーザーの意図した操作を過不足なく許可しているかどうかは、別個の忠実性の問題である。

---

## 簡易確認 — ゲートウェイは本当にエージェントを制御しているか

対応フレームワーク全体で、固定の強制攻撃に対する統制検査を実行する。オフラインのフレームワークに API キーは不要:

```bash
.venv/bin/python -m eval.check
```

```
framework     permitted   attacks  over-rej  tasks  result
shopping              0         8         0      2  PASS
dining                0         7         0      2  PASS
injecagent            0      1598         0   1054  PASS
banking               0       135         0     13  PASS
...
RESULT: PASS -- no tested forced injection was permitted.
```

この結果が対象とするのはハーネスが生成したラベル付き呼び出しである。未知の攻撃や、コンパイル済み policy が許可しうるすべての呼び出しに対する証明ではない。現在の評価は、必要な許可呼び出しと過剰な許可呼び出しを、一つの参照忠実性誤差の二方向として比較する。

**フレームワーク横断・モデル横断の完全な結果は
[`docs/EVALUATION.md`](docs/EVALUATION.md) にある** — 厳密な参照認可、実行時診断、人間認可経路を含む。

---

## デプロイ

ゲートウェイをエージェントと実ツールの間に置く**運用者**向け。(実験の再現だけが目的なら [再現](#再現) へ。)

> **保護水準に関する注意:** Bash が有効な Claude Code では、実効的な防御は L1–L2 に留まる。
> L3 は、生の Bash・直接のネットワーク I/O・観測されない資格情報の経路をエージェントに
> 与えないことが前提である。前提条件は [`docs/SELF_HOSTING.md`](docs/SELF_HOSTING.md) の
> 「保護の前提条件」節、対象外の範囲は [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) の「範囲外」節を参照。

どの場合も手順は四つ: **インストール → デーモンの起動 → エージェントを専用の非管理者ユーザーで実行 → そのユーザーの外向き通信をゲートウェイのみに制限。** エージェントがゲートウェイに*到達する*方法は、実行場所によって異なる:

| 状況 | エージェントがゲートウェイに到達する方法 |
|-----------|-----------------------------------|
| **A. ローカルエージェント**(Claude Code / 手元のマシン上のスクリプト) | 設定を自分が所有しているので、フック経由でプロンプトとツール呼び出しをゲートウェイに直接渡す。 |
| **B. クラウド / API エージェント**(プロバイダ上で動き、API 越しに駆動される) | プロセスを所有していないので、ゲートウェイがツールと資格情報の境界になる。 |

### 前提

リポジトリと仮想環境を用意する:

```bash
git clone https://github.com/Aj1905/PAuthGateway.git && cd PAuthGateway && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

自分のクライアントだけがデーモンを駆動できるよう、共有認証トークンを生成する:

```bash
export GATEWAY_AUTH_TOKEN="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

ケース A ではさらに、管理者権限 / `sudo`(ネットワーク設定の一度だけ使用)と、対応するファイアウォール — Linux の `nftables`/`iptables` または macOS の `pf` — が必要。

### デーモンの起動(両ケース共通)

ループバックに束縛し、すべてのルートでトークンを要求する:

```bash
.venv/bin/python gateway/serving/http_server.py --host 127.0.0.1 --port 8081 --auth-token "$GATEWAY_AUTH_TOKEN"
```

起動状態を維持すること(`systemd` ユニット、`launchd` ジョブ、`tmux` ウィンドウなど)。有用なフラグ:
`--session-store PATH` で再起動をまたいでセッションを維持、`--audit-log PATH` で許可/拒否の判定を JSONL として追記。死活確認(このルートにトークンは不要):

```bash
curl http://127.0.0.1:8081/health
```

> **ゲートウェイはエージェントとは*別の* OS ユーザーで実行すること。** ゲートウェイは実際の SaaS に到達する必要があり、ケース A の外向き制限は意図的にゲートウェイには適用されない。

### ケース A — ローカルエージェント

Claude Code が汚染されていないプロンプトと各ツール呼び出しをゲートウェイに渡すよう、フックを二つ追加する(Claude Code 本体の変更も、プロンプトの入力方法の変更も不要)。`~/.claude/settings.json` に:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "type": "command", "command": "/ABSOLUTE/PATH/PAuthGateway/gateway/hooks/submit_prompt.sh" }
    ],
    "PreToolUse": [
      { "type": "command", "command": "/ABSOLUTE/PATH/PAuthGateway/gateway/hooks/pretool.sh" }
    ]
  }
}
```

`submit_prompt.sh` はモデルがプロンプトを見る**前に**それを転送する(計画は汚染されていないタスクから構築される)。`pretool.sh` は**すべての**ツール呼び出しを許可/拒否の検査にかける。`export
GATEWAY_URL=http://127.0.0.1:8081` と同じトークンでデーモンに向ける。オプションは
[`gateway/hooks/README.md`](gateway/hooks/README.md) を参照。Claude Code でなくても、任意のローカルエージェントがプロンプトを一度、続いて各ツール呼び出しを `/sessions/<id>/messages` に `POST` すればよい
([`docs/SELF_HOSTING.md`](docs/SELF_HOSTING.md#プロンプト捕捉の境界))。

専用の非管理者エージェントユーザーを作成し、そのユーザーの外向き通信をゲートウェイのみに制限し(管理者権限が必要、一度だけ実行)、以後エージェントをそのユーザーとして実行する:

```bash
sudo useradd -m -s /bin/bash pauth-agent   # macOS: sysadminctl -addUser pauth-agent
sudo AGENT_USER=pauth-agent GATEWAY_HOST=127.0.0.1 GATEWAY_PORT=8081 gateway/deploy/egress_lockdown.sh apply
sudo -u pauth-agent claude
```

> **エージェントを非管理者に保つこと。** 管理者権限を与えた瞬間にこの制御は迂回可能になる。詳細と前提条件は [`docs/SELF_HOSTING.md`](docs/SELF_HOSTING.md) の「Egress Lockdown」節を参照。

### ケース B — クラウド / API エージェント

エージェントは自分の制御下にないプロバイダ上で動く。変わるのは二点: (1) 固定できるローカル UID が存在しないため、相当する統制は、ゲートウェイを**エージェントが到達できる唯一のツールエンドポイント**にし、実際の SaaS トークンがゲートウェイの内側だけに存在するよう資格情報を仲介すること。(2) プロンプトの捕捉には**ゲートウェイが所有する入口**を使う — まずタスクをゲートウェイに提出し(`POST /sessions/<id>/messages`)、その後エージェントのツール呼び出しをゲートウェイ経由で実行させる。

正直な注意: クラウドエージェントが任意の URL を呼び出せて、その外向き通信も資格情報も制約できない場合、純粋な API 上の関係で得られるのは**観測と宛先単位の許可/拒否であって、完全な執行ではない**。
[`docs/SELF_HOSTING.md`](docs/SELF_HOSTING.md#設定の境界) を参照。

---

## 再現

インストール:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Python 3.12+(3.14 で検証済み)。

### オフライン — API キー不要

決定的な中核のゼロ FP / ゼロ FN を、論文の実例(banking 5.3 節、shopping 4 節)に対して検証する:

```bash
.venv/bin/python -m tests.test_worked_examples
```

攻撃(スライス外の操作、改ざんされた宛先/金額/日付、改ざんされた envelope)を、実際の AgentDojo ツール上の Enforcer に直接ぶつける:

```bash
.venv/bin/python -m tests.test_unexpected_attacks
```

厳密に解釈すること: PAuth はスライス外の攻撃を拒否するが、**正規のスライスに厳密に一致するリプレイは許可される** — それは PAuth の認可境界であって、バグではない。

### 完全実験 — OpenAI API キー

Planner(GPT-4.1)を四つの AgentDojo スイートに対して実行し、FP/FN を測定する:

```bash
cp .env.example .env   # write OPENAI_API_KEY, then:
.venv/bin/python -m eval.fpfn --suites all
```

まず安価に試す(一つのスイートの先頭 3 タスク):

```bash
.venv/bin/python -m eval.fpfn --suites banking --limit 3
```

パラメータ化された funnel は、各段階の診断、参照忠実性の両半分、結果、攻撃プローブ、費用を、モデルとモードを横断して測定する([`docs/EVALUATION.md`](docs/EVALUATION.md) を参照):

```bash
.venv/bin/python -m eval.funnel agentdojo --mode headless --planner bestof --model gpt-5.1 --structuring
```

`eval.fpfn` のオプション: `--suites banking,shopping`(スイートの選択)、`--limit N`、
`--model gpt-4.1`、`--no-cache`、`--out path.json`。費用 ≈ $0.002–0.04/タスク
(97 タスク全体で約 $1–4)。生成された計画は `tests/experiment/cache/` にキャッシュされるため、再実行は無料。

### 旧 `eval.fpfn` の構成要素診断

- **過剰拒否診断** — 生成された計画を実行し、すべての呼び出しを Enforcer に通す。一件でも拒否があれば、そのタスクに印を付ける。
- **強制攻撃診断** — オペランドを改ざんした、あるいはタスク外のラベル付き呼び出しを、実行後の envelope ストアに対して Enforcer に提示する。許可されれば、その事例に印を付ける。

これらの旧診断は有限の構成要素プローブであり、policy の過剰/過少許可の関係全体でも `REF_EXACT_AUTHORIZATION` でもない。スライス上の強制呼び出しは許可されうる。これはハーネスが単にすべてのプローブを拒否しているわけではないことを示しており、そうしたリプレイが悪意あるものかどうかは、ベンチマークのラベルと脅威モデルに依存する。

---

## リポジトリ構成

```
pauth/              PAuth core (framework-independent, mostly deterministic)
  grammar.py          restricted-grammar parser / validator (paper Appendix A)
  codegen.py          Planner: restricted-grammar code generation (OpenAI)
  slicing.py          Slicer: natural-language slice derivation
  rules.py            Rule compiler: Algorithm 1
  evaluator.py        deterministic evaluator for slice expressions
  enforcer.py         Enforcer: runtime authorization + sandboxed executor
  envelope.py         signed-envelope structure, HMAC signing, store
  pipeline.py         Planner → Slicer → Rule compiler wiring
  suites/shopping.py  the paper's self-contained Shopping suite
gateway/
  serving/http_server.py       local HTTP daemon
  hooks/                        Claude Code prompt + tool-call hooks
  deploy/egress_lockdown.sh     per-user egress restriction
  planning/agentic_planner.py   Planner with grammar + semantic self-repair
  runtime/confirmation.py       confirmation-gate machinery (untrusted-derived operands)
  runtime/confirmer.py          confirmer strategies (informed / cautious / rubber-stamp)
  runtime/human_authorized.py   human-authorization path: single-use, bound, signed grants
benchmarks/
  agentdojo_adapter.py          normalizes the 4 AgentDojo suites to one interface
  forced_injection.py           forced-injection generation (sec. 5.1)
  injecagent_adapter.py, tau_bench_adapter.py   additional framework adapters
eval/
  check.py            one-command fixed forced-attack control check
  fpfn.py             FP/FN + acceptance runner (paper Table 2 / Fig. 10)
  funnel.py           parameterized lifecycle + reference-fidelity evaluation
  metrics.py          canonical metric vocabulary and reporting groups
  gates.py            per-metric attribution (see docs/GLOSSARY.md)
docs/
  EVALUATION.md       central claim, results, limitations  ← start here for the science
  ARCHITECTURE.md     whole-system logical design
  THREAT_MODEL.md     defense boundary (in / out of scope)
  GLOSSARY.md         precise definitions
```

各文書の役割はファイル名が示す。ここでは目的別の読み順と、`docs/` の外にある
文書だけを挙げる。

- **導入する人**: [README](README.md)（本書）→ [`docs/SELF_HOSTING.md`](docs/SELF_HOSTING.md) → [`gateway/hooks/README.md`](gateway/hooks/README.md)
- **設計を知りたい人**: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) → [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) → [`docs/DESIGN_STATUS.md`](docs/DESIGN_STATUS.md)
- **評価を知りたい人**: [`docs/EVALUATION.md`](docs/EVALUATION.md) → [`docs/GLOSSARY.md`](docs/GLOSSARY.md) → [`docs/REF_REQUIRED_IMPROVEMENT_LOG.md`](docs/REF_REQUIRED_IMPROVEMENT_LOG.md)（履歴）
- **論文執筆**: [`paper/PAuthGateway/AUTHORING_GUIDE.md`](paper/PAuthGateway/AUTHORING_GUIDE.md)

---

## 論文との対応

| 論文 | 本実装 |
|-------|---------------------|
| 命令型コード生成(LLM、4.1.1 節) | Planner — `pauth/codegen.py`(OpenAI、Appendix A のプロンプト) |
| NL スライス導出(3.3 / 4.1.2 節、決定的) | Slicer — `pauth/slicing.py` |
| ルールコンパイル(Algorithm 1、決定的) | Rule compiler — `pauth/rules.py` |
| 署名付き envelope(3.4 節 / 図 3) | `pauth/envelope.py` |
| 実行時執行(4.1.3 節、決定的) | Enforcer — `pauth/enforcer.py` |
| 制限文法(BNF、Appendix A) | `pauth/grammar.py` |
| AgentDojo 実装(4.1 節) | `benchmarks/agentdojo_adapter.py` |
| 強制注入(5.1 節) | `benchmarks/forced_injection.py` |
| FP/FN 評価(5.2 節、Table 2) | `eval/fpfn.py` |

論文と同様、**LLM を必要とするのは Planner だけ**であり、スライス導出、ルールコンパイル、執行、envelope は完全に決定的である(論文 5.2 節)。

## 再現の範囲

- **Planner のモデル** — 論文は主に GPT-4.1 を用いる(GPT-5-Mini /
  Gemini-3-Flash / Sonnet-4.5 は部分的)。本実装は OpenAI 系を既定とし
  (`--model` で切替)、ここでの結果は GPT-5.1 も対象とする。
- **envelope の署名** — 論文はホスト間で署名付き envelope を交換するが、本実装は
  AgentDojo に合わせた単一ホスト構成であり、共有メモリの envelope ストア上で
  HMAC を用いる。
