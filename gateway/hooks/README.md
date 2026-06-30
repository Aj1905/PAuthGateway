# Claude Code hooks for the PAuth gateway

このディレクトリは、未改変の Claude Code セッションの周囲でゲートウェイを実際の
ファイアウォールに変える、2つの Claude Code hook スクリプトを提供する。

これらの hook はゲートウェイのセットアップの一部だ。Claude Code のランタイムも
ユーザの通常のプロンプトワークフローも変更しないが、それでも明示的な統合要件
である。プロンプト hook と pre-tool hook がなければ、ゲートウェイはクリーンな
計画を確実に構築することも、試行されたアクションが実行される前に強制することも
できない。

| Hook | Script | Purpose |
|---|---|---|
| `UserPromptSubmit` | `submit_prompt.sh` | LLM がプロンプトを見る**前に**、ユーザのプロンプトを計画生成のためゲートウェイへ転送する。 |
| `PreToolUse` | `pretool.sh` | すべてのツール呼び出しをゲートウェイへ提示する。ゲートウェイはそれを有効な計画と照合し、許可または拒否する。 |

両方の hook は、`localhost` 経由で長時間稼働する HTTP daemon
(`gateway/serving/http_server.py`) と通信する。セッション状態は Claude Code 自身の
`session_id` をキーとするため、2つの hook は会話の継続中、同じゲートウェイ
セッション上で動作する。

## 1. Start the gateway

```bash
.venv/bin/python gateway/serving/http_server.py --host 127.0.0.1 --port 8081
```

これは稼働させたままにする。再起動するとすべての有効なセッションが失われる。

## 2. Add the hooks to Claude Code settings

`~/.claude/settings.json`（またはプロジェクトローカルの `.claude/settings.json`）を
編集する。

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

絶対パスは自分のチェックアウトに合わせて調整する。

## 3. Choose enforcement mode

スクリプトは環境変数を尊重する。

| Variable | Values | Default | Effect |
|---|---|---|---|
| `GATEWAY_URL` | URL | `http://127.0.0.1:8081` | POST 先。 |
| `GATEWAY_MODE_PROMPT` | `strict` / `log` | `strict` | ゲートウェイがプロンプトを拒否した場合、Claude Code をブロックする（`strict`）か、ログだけ取って続行する（`log`）か。 |
| `GATEWAY_MODE_TOOL` | `strict` / `log` | `log` | ツール呼び出しに対する同様の設定。統合の検証中はデフォルトを `log` にし、強制対象のツール集合が確定したら `strict` へ切り替える。 |
| `GATEWAY_MODE` | `strict` / `log` | — | より具体的な変種が未設定のときのフォールバック。 |
| `PAUTH_PLANNER_STRATEGY` | `deterministic` / `llm-freeform` / `interactive-structuring` / `specialized-codegen` / `formal-semantic` | `deterministic` | A1 の計画 strategy を選ぶ。 |
| `PAUTH_PLANNER_SUITE` | suite name | — | `llm-freeform` で必須。例: `shopping`。 |
| `PAUTH_PLANNER_MODEL` | model id | `gpt-4.1` | LLM 裏付けの strategy 用モデル。 |
| `PAUTH_PLANNER_MAX_RETRIES` | integer | `3` | validator フィードバックループのリトライ予算。 |
| `PAUTH_PLANNER_ENABLE_JUDGE` | boolean | `true` | `llm-freeform` 向けの意味的 judge を有効化する。 |

これらはシェルの rc、hook コマンド自体、あるいは Claude Code の `env` ブロックで
設定する。planner 変数は gateway daemon 側でもプロンプト hook 側でも設定でき、
`submit_prompt.sh` は存在する場合それらをプロンプトメッセージで転送する。

free-form planner の例:

```bash
PAUTH_PLANNER_STRATEGY=llm-freeform \
PAUTH_PLANNER_SUITE=shopping \
.venv/bin/python gateway/serving/http_server.py --host 127.0.0.1 --port 8081
```

## 4. Verify the round-trip

daemon を稼働させ hook をインストールした状態で、Claude Code を開き、正規の
Aurora プロンプトを打ち込む。

> If the product "Aurora Noise Cancelling Headphones" is in stock and
> costs less than $150.00, add 1 to my cart and pay the cart total to
> IBAN GB33BUKB20201555555555 with subject "Order payment" on
> 2024-06-11.

`submit_prompt.sh` hook は Claude Code のトランスクリプト／stderr に
`prompt accepted ::` をログ出力し、gateway daemon の端末には POST が表示され、
続くツール呼び出しが `pretool.sh` 経由で現れるはずだ。

## Failure modes to expect

* **Gateway daemon down** → hook が HTTP エラーを表示する。`strict` モードでは
  これがブロックし、`log` では許可する。自動再起動はない。
* **Prompt outside the deterministic recognizer subset** → ゲートウェイが拒否し、
  `strict` モードでは即座に Claude Code をブロックする。`PAUTH_PLANNER_SUITE` を
  設定したうえで `PAUTH_PLANNER_STRATEGY=llm-freeform` へ切り替えるか、recognizer を
  拡張する。
* **Registered strategy not implemented** → `interactive-structuring`、
  `specialized-codegen`、`formal-semantic` は現状、明示的に拒否する。これらは
  将来の作業のための名前付き枠であって、フォールバックではない。
* **Tool not in the plan** → `pretool.sh` が REJECT を報告する。`strict` モードでは
  Claude Code はそのツールを実行できない。`log` モードでは続行するが、拒否はログに
  残る。強制にコミットする前に、実際の Claude Code の挙動を測定するのに有用だ。

システム全体の設計については `architecture.md` を、決定の履歴（Q10 capture
mechanism、Q13 trust shift など）については `grill.md` を参照。
