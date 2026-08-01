# PAuth ゲートウェイのための Claude Code hook

このディレクトリは、無改造の Claude Code セッションの周囲でゲートウェイを
実際のファイアウォールとして機能させる、二つの Claude Code hook スクリプトを
提供する。

これらの hook はゲートウェイのセットアップの一部である。Claude Code の
ランタイムもユーザーの通常の prompt ワークフローも変更しないが、それでも
明示的な統合上の必須要件である。prompt hook と pre-tool hook がなければ、
ゲートウェイはクリーンな計画を確実に構築することも、試行された操作を実行前に
強制することもできない。

| Hook | スクリプト | 目的 |
|---|---|---|
| `UserPromptSubmit` | `submit_prompt.sh` | LLM が prompt を見る**前**に、ユーザーの prompt を計画生成のためゲートウェイへ転送する。 |
| `PreToolUse` | `pretool.sh` | すべてのツール呼び出しをゲートウェイに提示する。ゲートウェイは有効な計画と照合し、許可または拒否する。 |

両方の hook は `localhost` 上で常駐 HTTP デーモン
(`gateway/serving/http_server.py`)と通信する。セッション状態は Claude Code
自身の `session_id` をキーとするため、二つの hook は会話の間、同じゲートウェイ
セッションに対して動作する。

## 1. ゲートウェイを起動する

```bash
.venv/bin/python gateway/serving/http_server.py --host 127.0.0.1 --port 8081
```

これは起動したままにしておく。再起動するとすべての有効なセッションが失われる
(`--session-store PATH` で計画再構築の入力を永続化できる。旧 `issues` B1)。

任意で `--audit-log PATH` を追加すると、permit/deny/accept/reject の判定が
JSONL として追記される(運用者向け; 値を含みうるため、エージェントが読めない
場所に置く)。死活確認は `curl http://127.0.0.1:8081/health`、セッション状態は
`curl http://127.0.0.1:8081/sessions/<id>`(値を含まない保護レベル、計画の
有無、ルール数)で確認できる。

## 2. Claude Code の設定に hook を追加する

`~/.claude/settings.json`(またはプロジェクトローカルの
`.claude/settings.json`)を編集する。

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "type": "command", "command": "/Users/aj/Documents/PAuthGateway/gateway/hooks/submit_prompt.sh" }
    ],
    "PreToolUse": [
      { "type": "command", "command": "/Users/aj/Documents/PAuthGateway/gateway/hooks/pretool.sh" }
    ]
  }
}
```

絶対パスは自分のチェックアウトに合わせて調整すること。

## 3. 強制モードを選ぶ

スクリプトは環境変数に従う。

| 変数 | 値 | 既定値 | 効果 |
|---|---|---|---|
| `GATEWAY_URL` | URL | `http://127.0.0.1:8081` | POST の宛先。 |
| `GATEWAY_AUTH_TOKEN` | token | (未設定) | デーモンを `--auth-token` 付きで起動する場合、同じ値をここに設定すると hook が `Authorization: Bearer <token>` を送る。 |
| `GATEWAY_MODE_PROMPT` | `strict` / `log` | `strict` | ゲートウェイが prompt を拒否したとき、Claude Code をブロックする(`strict`)か、ログだけ残して続行する(`log`)か。 |
| `GATEWAY_MODE_TOOL` | `strict` / `log` | `log` | ツール呼び出しに対する同じ設定。統合の検証中は既定の `log` のままにし、強制対象のツール集合が固まったら `strict` に切り替える。 |
| `GATEWAY_MODE` | `strict` / `log` | — | より具体的な変数が未設定のときの代替。 |
| `PAUTH_PLANNER_STRATEGY` | `deterministic` / `llm-freeform` / `auto` / `sufficiency-tightness` / `interactive-structuring` / `specialized-codegen` / `formal-semantic` | `auto` | Planner 戦略を選ぶ(未設定なら `AgentChannel` の既定 `auto`)。 |
| `PAUTH_PLANNER_SUITE` | suite 名 | — | `llm-freeform` と `sufficiency-tightness` に必須。`auto` では LLM フォールバック先を有効にする。例: `shopping`。 |
| `PAUTH_PLANNER_MODEL` | model id | `gpt-4.1` | LLM を用いる戦略のモデル。 |
| `PAUTH_PLANNER_MAX_RETRIES` | 整数 | `3` | 検証器フィードバックループの再試行予算。 |
| `PAUTH_PLANNER_ENABLE_JUDGE` | 真偽値 | `true` | `llm-freeform`、`auto` の LLM フォールバック、`sufficiency-tightness` の意味判定器を有効化する。 |
| `PAUTH_PLANNER_JUDGE_MODEL` | model id | — | これらの意味判定器に別モデルを使う場合に指定(任意)。 |
| `PAUTH_PLANNER_CACHE_DIR` | path | — | 生成コードのキャッシュ先。**デーモン側の環境変数としてのみ有効**。wire 経由の値は任意ディレクトリ書き込み防止のため無視される(`agent_channel.py` の `parse_message` 参照)。 |

これらは shell の rc、hook のコマンド自体、または Claude Code の `env`
ブロックで設定する。Planner 系の変数は、`PAUTH_PLANNER_CACHE_DIR` を除いて
ゲートウェイデーモン側と prompt hook 側のどちらでも設定できる。設定されていれば
`submit_prompt.sh` が prompt メッセージに載せて転送する。キャッシュ先は
ゲートウェイデーモン側でのみ設定する。

正準名と生成処理の正本は `gateway/planning/planner.py` の `KNOWN_STRATEGIES` と
`build_planner()` である。この表は配備時に使う値だけを転記する。

自由形式 planner の例:

```bash
PAUTH_PLANNER_STRATEGY=llm-freeform \
PAUTH_PLANNER_SUITE=shopping \
.venv/bin/python gateway/serving/http_server.py --host 127.0.0.1 --port 8081
```

## 4. 往復を検証する

デーモンを起動し hook を導入した状態で Claude Code を開き、正当な Aurora
prompt を入力する。

> If the product "Aurora Noise Cancelling Headphones" is in stock and
> costs less than $150.00, add 1 to my cart and pay the cart total to
> IBAN GB33BUKB20201555555555 with subject "Order payment" on
> 2024-06-11.

`submit_prompt.sh` hook が Claude Code の transcript / stderr に
`prompt accepted ::` を記録し、POST がゲートウェイデーモンの端末に現れ、
以降のツール呼び出しが `pretool.sh` 経由で現れるはずである。

## 4b. エージェントの egress を封じる(バイパス防止)

hook が捕捉できるのは、エージェントが**協調的にツール呼び出しを提示する**
経路だけである。このバイパスを防ぐには、Claude Code を**専用の非管理者
ユーザー**として実行した上で、管理者権限で一度だけ次を実行し、以後エージェントを
そのユーザーとして起動する(例: `sudo -u pauth-agent claude`)。

```bash
sudo AGENT_USER=pauth-agent GATEWAY_HOST=127.0.0.1 GATEWAY_PORT=8081 gateway/deploy/egress_lockdown.sh apply
```

**エージェントに管理者権限を与えるとこの制御は無効になり、バイパス可能になる。**
詳細と前提条件は `docs/SELF_HOSTING.md` の「Egress Lockdown」節を参照。

## 想定される失敗モード

* **ゲートウェイデーモンの停止** → hook は HTTP エラーを表示する。`strict`
  モードではブロックし、`log` モードでは許可する。自動再起動はない。
* **決定的認識器の部分集合の外にある prompt** → ゲートウェイが拒否し、
  `strict` モードでは Claude Code を即座にブロックする。`PAUTH_PLANNER_SUITE`
  を設定して `PAUTH_PLANNER_STRATEGY=llm-freeform` に切り替えるか、認識器を
  拡張する。
* **登録済みだが未実装の戦略** → `interactive-structuring`、
  `specialized-codegen`、`formal-semantic` は `PlanGenerationError` となり、
  `accepted=false`、`rule_count=0` を返す。Gateway には計画生成失敗を記録した
  セッションが残り、同じ channel では再計画できない。これは Planner の計画生成失敗であり、
  Enforcer の拒否ではない。
* **計画にないツール** → `pretool.sh` は REJECT を報告する。`strict` モード
  では Claude Code はそのツールを実行できない。`log` モードでは続行するが、
  拒否はログに記録される。強制に踏み切る前に Claude Code の実際の挙動を
  測るのに有用である。

システム全体の設計は `docs/ARCHITECTURE.md` を参照。
