# Self-hosted gateway direction

このドキュメントは、現行の PAuth ゲートウェイを、既存のエージェントユーザが
日々のエージェントワークフローを変えずに接続できる自己ホスト型アプリへと
仕立てるための、プロダクト目標とエンジニアリング境界を記録する。

## Target

ユーザはエージェントのプロンプト・エージェントのコード・ツール定義のいずれも
編集すべきではない。目標はこうだ。初期セットアップ後、ユーザの通常のエージェント
ワークフローは変わらないように感じられ、その裏でゲートウェイがすべての外向き
アクションを観測し強制する。

初期セットアップでは、ゲートウェイ統合のインストール／有効化を行ってよい。
実際にはこれは次の両方を意味する。

1. クリーンなユーザプロンプトと試行されたツール呼び出しをゲートウェイへ転送する
   ライフサイクル hook/プラグイン。
2. エージェントが外向きアクションでゲートウェイをバイパスするのを防ぐ
   ネットワーク／ツール経路。

この目標が成立するのは、ゲートウェイが次の2つを観測できるときに限られる。

1. ツール結果のインジェクションが計画に影響を与える前の、ユーザのタスクプロンプト。
2. 具体的なツール名と引数を伴う、すべての外向きツール呼び出し。

対象エージェントがどちらか一方でも、hook/プロキシで観測可能なプロトコル境界を
通さずに暗号化・隠蔽・内部実行する場合、透過的なネットワーク設定だけでは PAuth
強制を提供できない。せいぜい宛先単位の粗い allow/deny を提供できるにとどまる。

## Setup Boundary

近期で最良のプロダクト契約は「ゼロセットアップ」ではない。次のものだ。

- **No agent code changes**: エージェントのバイナリ／ランタイムは未改変のまま。
- **No prompt workflow changes**: ユーザは引き続きエージェントにタスクを打ち込む。
- **Gateway setup required once**: hook/プラグインのインストール／有効化、gateway URL
  の設定、strict/log モードの設定、そして登録済みツール／API 呼び出しをゲートウェイ
  経由に経路付けする。
- **Observable health**: ゲートウェイは hook とツール経路が有効かどうかを可視化
  しなければならない。サイレント障害は無保護より悪い。

セットアップがより軽いことを約束する代替案は、いずれも弱い。

| Alternative | Why it is not enough |
|---|---|
| Pure network/TLS proxy | クリーンなプロンプト・意味的なツール名・構造化された引数を回復できないのが通例。 |
| SaaS-side only enforcement | すべての SaaS が PAuth を採用するか、互換のポリシ hook を公開することを要求する。 |
| Agent-vendor native integration | UX は最良だが、ベンダー採用に依存し、自己ホスト側で制御できない。 |
| Browser/OS observation only | 脆く、決定的・可搬にするのが難しい。 |

したがって実用的なアーキテクチャは **gateway app plus agent integration** である。
ネットワークのファイアウォールだけがセキュリティ境界なのではなく、ライフサイクル
hook が PAuth に必要な意味的イベントを供給する。

## Protection Precondition: No Raw Side Channels (Stage 1)

Stage 1 の保護主張は次の前提でのみ成立する(決定記録: solution.md S6、B5)。

- エージェントは生 Bash・直接ネットワーク I/O・観測されない credential 経路を
  **持たない**。外向きアクションはすべて gateway 経由の tool call である。
- この前提を満たさないデプロイ(例: Bash が有効な Claude Code)は L3 保護として
  訴求してはならない。実効保護レベルを正直に L1/L2 として報告すること。
- 側チャネルの gate 機構(allowlist / sandbox / FS 仮想化 / 応答書き換え)は
  Stage 6(Mode 2)の議題であり、Stage 1 には含まれない。

## Prompt Capture Boundary

ゲートウェイが、あらゆるエージェントから単一のメカニズムでプロンプトを取得する
ことは現実的ではない。目標は「同じ捕捉メカニズム」ではなく、「捕捉後の同じ
正規化イベント」である。

すべての統合は、ネイティブなプロンプトシグナルを次の形に翻訳すべきだ。

```json
{
  "kind": "prompt",
  "session_id": "agent-session-id",
  "prompt": "clean user task text",
  "source": "claude-code-hook | mcp-session | browser-extension | desktop-plugin | manual",
  "captured_before_model": true
}
```

現行コードは `gateway/ingress/agent_channel.py` 内の `PromptMessage` を正規化
プロンプトイベントとして用いる。将来のプロンプト捕捉アダプタも、同じ境界へ
供給すべきである。

プロンプト捕捉の選択肢を、強い順から弱い順に並べる。

| Capture route | Strength | Problem |
|---|---|---|
| Agent lifecycle hook/plugin | 利用可能なときの近期最良の経路。ツール実行前にプロンプトを捕捉する。 | エージェントごとの統合作業が必要。 |
| Gateway-owned prompt entrypoint | 最高の完全性。ユーザはまずゲートウェイ経由でタスクを入力する。 | ユーザワークフローを変える。許容できるときのみ使う。 |
| MCP/session metadata | エージェントがタスクメタデータをツールサーバへ送る場合は有望。 | 普遍的に利用可能でも標準化されてもいない。 |
| Browser/desktop extension | hook を持たないエージェントを支援できる。 | 脆く、アプリ固有で、順序性を証明しにくい。 |
| Manual confirmation fallback | 安全上クリティカルなアクションに有用。 | ワークフローを変え、摩擦を加える。 |

ゲートウェイはセッションごとに保護レベルを追跡すべきである。

| Level | Observed by gateway | PAuth claim |
|---|---|---|
| L0 | ネットワーク宛先のみ | PAuth 保証なし。粗いファイアウォールのみ。 |
| L1 | ツール呼び出しのみ | 未知／ポリシ外のツールを拒否できるが、ユーザ意図の計画は導出できない。 |
| L2 | クリーンなプロンプト + ツール呼び出し | PAuth の計画強制が意味を持つ。 |
| L3 | クリーンなプロンプト + ツール呼び出し + ゲートウェイ実行ツール | 現行で最強のモデル。envelope の来歴が信頼できる。 |

プロダクトのメッセージングで L0/L1 を「完全な保護」と呼んではならない。
PAuth 流の主張は L2 から始まり、設計目標は L3 である。

## Architecture Shape

```text
agent runtime
  |
  | hook/plugin + network/tool route
  v
gateway ingress
  |
  | normalized PromptMessage / ToolCallMessage
  v
planner boundary (A1)
  |
  | restricted imperative code
  v
pauth.prepare() -> rules
  |
  | per-call enforcement
  v
upstream tool/SaaS/MCP server
```

安定した契約は正規化メッセージ境界である。

- `PromptMessage`: タスクプロンプトと planner オプション。
- `ToolCallMessage`: 具体的なツール名と、順序付きまたは名前付きの引数。

この境界より前のすべてはアダプタ／プロキシの作業だ。planner 境界より後の
すべては PAuth の決定的コアである。

## Planner Strategy

A1 ロジックは意図的に揮発的だ。ゲートウェイはこれを HTTP/プロキシ表面の一部
ではなく、置換可能な strategy として扱わなければならない。

strategy カタログは `gateway/PLANNING_STRATEGIES.md` にある。近期の3つの枠は
次のとおり。

- 「Grill me」スタイルの明確化ループによる対話的構造化。
- 専用の imperative-code 生成モデルと validator のリトライ。
- 狭い制御言語ドメイン向けの形式的自然言語解析。

ランタイムの strategy 切り替えは、現行の HTTP/hook デプロイでは
`PAUTH_PLANNER_STRATEGY` という名前になっている。正規の値は `deterministic`、
`llm-freeform`、`interactive-structuring`、`specialized-codegen`、
`formal-semantic` である。将来のパッケージ化アプリは、planner 境界を変えずに、
同じ名前を設定ファイルや UI 設定へ移せる。

現行の strategy:

- `DeterministicRecognizerPlanner`: 厳格な正規表現サブセット。テストや高信頼の
  デモに向く。
- `LLMFreeformPlanner`: 文法修復と任意の intent judge を備えた、汎用の
  prompt-to-code 生成。

将来の strategy は同じ planner の形を実装すべきである。

- ファインチューンされた prompt-to-code モデル。
- リモートの planner サービス。
- 人間がレビューする計画承認。
- suite 固有の planner。
- 検索と LLM を組み合わせたハイブリッド planner。

不変条件は、すべての strategy が制限された imperative な `run` コードを発行し、
続いて `pauth.prepare()` が文法を検証し、slice を導出し、rules をコンパイルする
ことである。いかなる planner も、その決定的検証をバイパスすることは許されない。

## Self-hosted Foundation

最小限の自己ホスト型アプリ:

1. **Configurable upstream registry**
   - MCP/HTTP/SaaS のツールソースを登録する。
   - ツールスキーマ・パラメータ順・戻りスキーマを保持する。
   - 明示的に名前空間化されない限り、ツール名の衝突を拒否する。
   - HTTP API については OpenAPI spec を `SuiteSpec` へ反映する。
   - 上流 API spec の変更を検出し、新しいツール表面を受け入れる前にユーザへ
     通知可能なレポートを発行する。

2. **Ingress adapters**
   - ツール境界が見える MCP/HTTP から始める。
   - Claude Code hooks はプロダクトコアではなく、互換アダプタとして残す。
   - プロトコル固有のアダプタは、エージェントによる書き換えを信頼せずに
     プロンプトとツール呼び出しを露出できるときにのみ追加する。

3. **Planner plugin boundary**
   - デプロイ／セッションごとに planner を選ぶ。
   - 生成コード・検証失敗・リトライ履歴を監査用に保存する。
   - 本番のデフォルト姿勢は、捏造ではなく不確実時拒否（reject-on-uncertain）に
     すべきである。

4. **Session and audit store**
   - プロンプト・選択された planner・生成コード・コンパイルされた rule サマリ・
     決定・拒否・上流呼び出し結果を永続化する。
   - envelope 署名鍵はデプロイにローカルで保つ。

5. **Operations surface**
   - ツールソースと planner モードのための単一設定ファイル。
   - 上流ツールと planner クレデンシャルのヘルスチェック。
   - オンボーディング中はソースごとに strict/log モード。
   - 設定済み OpenAPI ソースのための定期的な API-spec モニタ。

## API Spec Reflection And Change Notification

OpenAPI を裏付けとする suite は gateway 設定に登録できる。

```json
{
  "merged_suite_name": "user_default",
  "suites": [
    {
      "name": "billing",
      "kind": "openapi",
      "spec_path": "billing.openapi.json",
      "base_url": "https://api.example.com"
    }
  ]
}
```

gateway 起動時、`gateway/providers/openapi_suite.py` が現行の OpenAPI ドキュメントを
`SuiteSpec` へ反映する。operation はツールになり、パラメータと JSON ボディの
フィールドはツールのオペランドになり、レスポンススキーマは A1 のツールドキュメント
になる。

通知／更新ワークフローのためには、次を実行する。

```bash
.venv/bin/python -m gateway.api_spec_monitor \
  --config gateway.json \
  --state .gateway/api-spec-state.json \
  --update
```

このモニタは、変更された spec・追加／削除されたツール・変更されたパラメータ
リストを記述した JSON を発行する。自己ホスト型デプロイは、その JSON をメール・
Slack・アプリ UI 通知・あるいは再起動／リロードのワークフローへ配線できる。

現状の制限: 稼働中の `gateway/serving/http_server.py` は、変更された OpenAPI spec を
ホットリロードしない。次の層では、認証付きのリロードエンドポイント、または
ユーザが変更されたツール表面を受け入れた後にスーパーバイザ管理で再起動する
仕組みを追加すべきである。

## Non-goals For The First Cut

- 任意のエージェントに対する汎用 TLS MITM。運用上脆く、それ単体では意味的な
  ツール名や引数を回復できない。
- prompt-to-code の正しさの主張。PAuth は生成された計画を強制するのであって、
  その計画がユーザの意図を忠実に捉えていることを証明するわけではない。
- 完全な SaaS マルチテナントホスティング。近期目標は自己ホストの、単一ユーザ
  または単一チームのデプロイである。

## Immediate Engineering Order

1. `pauth/` を安定かつフレームワーク中立に保つ。
2. すべての A1 変種を `gateway.planner` の背後へ移す。
3. `AgentChannel` の JSON メッセージを内部の正規化プロトコルとして扱う。
4. 実際のエージェントトラフィックをそのプロトコルへ翻訳する ingress アダプタを
   構築する。
5. 正規化プロトコルが安定した後に、永続化／監査を追加する。
