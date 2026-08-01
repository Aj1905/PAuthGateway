# τ²-Bench: Evaluating Conversational Agents in a Dual-Control Environment

- **bib キー**: `barres2025tau2bench`(書誌の正本は `../PAuthGateway/references.bib`)
- **著者**: Victor Barres, Honghua Dong, Soham Ray, Xujie Si, Karthik Narasimhan(Sierra / University of Toronto / Vector Institute — PDF 1ページ目で確認)
- **初出**: arXiv:2506.07982 v1 = 2025-06-09
- **版履歴**: v1 のみ(2026-08-01 時点)
- **査読**: **なし** — arXiv preprint のまま。脚注は「Preprint. Under review.」。ライセンスは CC BY 4.0。原著 τ-bench 同様、査読採択の記録は確認できず。
- **ローカル PDF**: `2506.07982v1-tau2bench.pdf`(**v1**。PDF の arXiv スタンプで確認)
- **コード**: https://github.com/sierra-research/tau2-bench — τ-bench の後継リポジトリ。airline / retail の修正版タスクに加え telecom(dual-control)、banking_knowledge 等を収録。**τ³-bench(最新の修正済みタスク群)もこのリポジトリ内で配布**。τ³ に単独論文は無く、リポジトリ自体を引用物として扱う(`../Tau3Bench/`、bib キー `sierra2026tau3bench`、release v1.0.1 で固定)。
- **要約**: 既存ベンチはエージェントだけがツールを持つ single-control 環境だが、実運用(技術サポート等)ではユーザー側も世界状態を変更する。これを Dec-POMDP として定式化した dual-control ドメイン(Telecom)、原子部品からタスクを合成する検証可能なタスク生成器、環境と密結合したユーザーシミュレータを提供。no-user から dual-control に移ると性能が大きく落ちる(=ユーザーを誘導する能力が別軸の困難)と報告。
- **本プロジェクトとの関係**: τ-bench(`../TauBench/`)の後継。可用性・遵守系の現行水準を語るならこちらを引く。原著は pass^k 指標や問題設定の初出として引き分ける。旧 τ-bench のタスクは公式に非推奨である点に注意。

確認日: 2026-08-01(arXiv abs ページ・ローカル PDF 1ページ目・GitHub README で確認)
