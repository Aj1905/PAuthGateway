# リポジトリ指針

## 用語 — 解説は SYSTEM_MODEL.md に基づくこと

このアプリ(PAuth / ゲートウェイ)について解説・説明・文書を生成するときは、
必ず `docs/SYSTEM_MODEL.md` の定義に基づいて回答すること。

- 構成コンポーネントの名称と責務は、第 1 部(外殻: ingress アダプタ /
  AgentChannel / Gateway)と第 2 部(パイプライン: Planner /
  GrammarValidator / Slicer / Rule compiler / Enforcer / EnvelopeStore / ToolExecutor /
  ツールアダプタ)のノード定義に従う。粒度の粗い語(外殻、PAuth パイプライン
  など)は第 0 部で定義される。
- ノード間を流れる情報(prompt、run() コード、slice、rule、envelope、
  PendingConfirmation など)は第 3 部の定義に従い、ノード(部品)と
  情報(成果物)を混同しない。
- 失敗の語彙(文法棄却・クラッシュ・拒否・ツールエラー・Excess/Missing)は
  用語集第 2 部の「失敗の対照表」の区別を保ち、混同しない。
- A1–A4 / B1–B4 は論文(2603.17170)Figure 6 の矢印ラベルとしてのみ使う。
  地の文では空間ノード名+「計画時/実行時」の二相で書く。
- 評価指標は第 4 部の現行名(`FEASIBILITY_*` / `SYNTHESIS_*` / `REF_*` など)
  を使う。
- 用語集にない概念を新しく使う必要が生じたら、先に `docs/SYSTEM_MODEL.md` へ
  定義を追加してから本文で使う。

注: `AGENTS.md` は本ファイルと同一内容。片方を変更したら他方も同期すること。
