# τ-bench: A Benchmark for Tool-Agent-User Interaction in Real-World Domains

- **bib キー**: `yao2024taubench`(書誌の正本は `../PAuthGateway/references.bib`)
- **著者**: Shunyu Yao, Noah Shinn, Pedram Razavi, Karthik Narasimhan(Sierra)
- **初出**: arXiv:2406.12045 v1 = 2024-06-17
- **版履歴**: v1 のみ(改訂なし)
- **査読**: **なし** — 2026-08-01 時点で arXiv preprint のまま。公式リポジトリの推奨引用も `@misc`(arXiv)。企業(Sierra)発で、査読会議への採択記録は確認できず。
- **ローカル PDF**: `2406.12045v1-taubench.pdf`(**v1**。PDF の arXiv スタンプで確認。脚注は「Preprint. Under review.」のまま)
- **コード**: https://github.com/sierra-research/tau-bench
  ⚠️ **公式リポジトリ自身が「本リポジトリのタスクは古い。最新の修正済みタスクと新ドメインは τ³-bench を使え」と警告**(https://github.com/sierra-research/tau2-bench)。論文の数値と現行タスクは一致しない前提で扱うこと。
- **要約**: ツール実行だけでなく「ユーザーとの対話」と「ドメイン規約の遵守」を同時に試すベンチマーク。airline / retail ドメイン。最先端の function-calling エージェント(gpt-4o)でもタスク成功率 50% 未満と報告。
- **後継**: τ²-Bench(`../Tau2Bench/`、bib キー `barres2025tau2bench`)。現行の数値・タスクを語るなら後継を引き、本論文は pass^k 指標と問題設定の初出として引き分ける。
- **本プロジェクトとの関係**: セキュリティ系ではなく可用性・遵守系の比較対象。AVAIL 系メトリクスの文脈で引く際は、上記のタスク陳腐化の注意が引用の正確性に直結する。

確認日: 2026-08-01(arXiv abs ページ・GitHub README で確認)
