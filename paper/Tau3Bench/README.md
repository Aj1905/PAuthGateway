# τ³-bench(単独論文なし — リポジトリを引用物として扱う)

- **bib キー**: `sierra2026tau3bench`(書誌の正本は `../PAuthGateway/references.bib`)
- **実体**: 論文ではなく Git リポジトリ https://github.com/sierra-research/tau2-bench 。τ³-bench は「第3世代 τ ファミリー」の総称で、単独の論文は存在しない(2026-08-01 に web 検索で不在を確認)。構成要素の論文は以下の3本+原著:
  - τ²-Bench(arXiv:2506.07982、`../Tau2Bench/` に PDF あり)— dual-control / telecom
  - τ-Knowledge(arXiv:2603.04370)— banking_knowledge(非構造化知識検索)ドメインの初出
  - τ-Voice(arXiv:2603.13686)— 全二重音声トラック
  - 原著 τ-bench(arXiv:2406.12045、`../TauBench/`)— airline / retail の修正版を収録
- **引用の固定点**: **release `v1.0.1`(2026-07-22 公開)**。タグ: https://github.com/sierra-research/tau2-bench/releases/tag/v1.0.1 。確認時点の main は commit `363133a`(2026-07-29)で、release 後もリポジトリは動き続けている。実験に使うなら release タグ(または commit ハッシュ)を論文中に明記すること。
- **査読**: **なし**(リポジトリ本体には査読という概念自体がない。構成論文も 2026-08-01 時点で全て preprint)
- **⚠️ 採点の破壊的変更**: v1.0.1 のリリースノート自身が「banking_knowledge の採点方式を修正したため、**スコアは release をまたいで比較不能**。再採点でモデルにより pass^1 が最大約9ポイント上方に動く」と明記。τ 系の数値を引用・比較するときは、どの release で測った値かまで一致させないと比較が無効になる。
- **要約**: airline / retail(修正版)+ telecom(dual-control)+ banking_knowledge(RAG)+ 音声トラックを収録した、τ ファミリーの現行タスク集。旧 τ-bench リポジトリは公式に非推奨で、これが唯一の保守対象。
- **本プロジェクトとの関係**: 可用性・遵守系の実験を τ 系でやるなら使うべきはこれ。引用は「問題設定・指標 = 原著 τ-bench」「dual-control の手法 = τ²-Bench」「タスク成果物 = 本 bib キー(release 固定)」の三分割になる。「論文の数値と現行成果物の乖離」を論じる際の実例としても引ける。

確認日: 2026-08-01(GitHub API で release / commit を確認、web 検索で単独論文の不在を確認)
