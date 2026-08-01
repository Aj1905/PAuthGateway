# リポジトリ指針

## 用語 — 解説は GLOSSARY.md に基づくこと

このアプリ(PAuth / ゲートウェイ)について解説・説明・文書を生成するときは、
必ず `docs/GLOSSARY.md` の定義に基づいて回答すること。

- 構成コンポーネントの名称と責務は、用語集第 1 部のノード定義
  (Planner / GrammarValidator / Slicer / Rule compiler / Enforcer /
  EnvelopeStore / ツール供給源)に従う。
- ノード間を流れる情報(prompt、run() コード、slice、rule、envelope、
  PendingConfirmation など)は第 2 部の定義に従い、ノード(部品)と
  情報(成果物)を混同しない。
- 失敗の語彙(文法棄却・クラッシュ・拒否・ツールエラー・Excess/Missing)は
  用語集第 1 部の「失敗の対照表」の区別を保ち、混同しない。
- A1–A4 / B1–B4 は論文(2603.17170)Figure 6 の矢印ラベルとしてのみ使う。
  地の文では空間ノード名+「計画時/実行時」の二相で書く。
- 評価指標は第 3 部の現行名(`FEASIBILITY_*` / `SYNTHESIS_*` / `REF_*` など)
  を使う。
- 用語集にない概念を新しく使う必要が生じたら、先に `docs/GLOSSARY.md` へ
  定義を追加してから本文で使う。

注: `AGENTS.md` は本ファイルと同一内容。片方を変更したら他方も同期すること。
