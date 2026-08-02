# PAuth Gateway への貢献

関心を持っていただき感謝します。PAuth Gateway は AI エージェント向けのタスク範囲限定の認可ゲートウェイ — すなわちセキュリティツールであり、PAuth 論文の再現と拡張です
(`NOTICE` を参照)。貢献は Apache-2.0 ライセンスの下で歓迎します。

## セキュリティツールとしての基本原則

本プロジェクトの要点は一つの保証にある: **侵害されたエージェントは、承認済み計画を超えて行動できない**(過剰認可なし)。これを守る二つの不変条件がある —
プルリクエストで明確な根拠を示さずに弱めてはならない:

1. **決定的な中核は決定的なままにする。** どの Planner 戦略も、`pauth.prepare()` が
   構文解析・スライス導出・ルールコンパイルを行う制限付き run コードを出力する。
   その検証を迂回したり、ルールを直接出力したりする Planner は認めない
   (`docs/SYSTEM_MODEL.md` の Planner / DSLValidator と `docs/SYSTEM_MODEL.md` 第 6 部「結合境界と帰結」を参照)。
2. **エージェント向けフィードバックは値を含まないままにする。** エージェントのモデル文脈に
   再流入する拒否理由は、オペランド値を一切含んではならない — プロンプトインジェクションの
   ペイロードになりうるためである。`gateway/runtime/feedback.py` を参照。

## リポジトリ構成

- `pauth/` — フレームワーク非依存のアルゴリズム中核: DSL、スライス導出、
  ルールコンパイル、Enforcer、署名付き envelope、Planner コード生成。
- `gateway/` — 実行時: Planner 戦略、呼び出し単位の執行、ツールプロバイダ
  (MCP / OpenAPI / スイート)、HTTP 配信、入口処理、デプロイスクリプト、
  Claude Code フック。
- `eval/` — 測定ランナー(FP/FN、E2E、評価ファネル)。
- `tests/` — 単体テストに加えて、実験アダプタとフィクスチャ。
- `docs/` — 設計文書: アーキテクチャ、脅威モデル、自己ホスティング、入口設計、
  Planner 戦略候補、設計状況。

## 開発環境の準備

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e .
```

`OPENAI_API_KEY`(および任意で `ANTHROPIC_API_KEY`)が必要になるのは
LLM の planner / 判定器の経路だけで、決定的なテストとオフラインのテストはキーなしで
動く。使う場合は `.env.example` を `.env` にコピーする。

## テストの実行

```bash
.venv/bin/python -m pytest tests/ -q                          # full suite, offline
.venv/bin/python -m eval.fpfn --suites shopping               # FP/FN measurement, no key
```

PR の前にすべてのテストが通ること。新しい挙動にはテストが必要であり、セキュリティに関わる
変更(執行、フィードバック、汚染追跡、サイドチャネル、外向き通信)には、修正がなければ
失敗するテストが必要。

## プルリクエスト

- `main` から分岐し、各 PR は焦点を絞ること。
- 周囲のコードスタイルに合わせること。コメントには制約を書き、変更の経緯を
  語らないこと。
- 執行、フィードバック、汚染追跡、サイドチャネルの方針に触れる変更では、
  PR の説明にセキュリティ上の根拠を書くこと。
- プルリクエストの提出により、貢献を Apache-2.0 ライセンスの下で提供することに
  同意したものとみなす。

## 脆弱性の報告

セキュリティ上の問題について、公開 issue を**開かないこと**。GitHub の非公開
脆弱性報告を使う — `SECURITY.md` を参照。
