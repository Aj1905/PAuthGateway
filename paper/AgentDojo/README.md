# AgentDojo: A Dynamic Environment to Evaluate Prompt Injection Attacks and Defenses for LLM Agents

- **bib キー**: `debenedetti2024agentdojo`(書誌の正本は `../PAuthGateway/references.bib`)
- **著者**: Edoardo Debenedetti, Jie Zhang, Mislav Balunović, Luca Beurer-Kellner, Marc Fischer, Florian Tramèr(ETH Zurich / Invariant Labs — PDF 1ページ目で確認)
- **初出**: arXiv:2406.13352 v1 = 2024-06-19
- **版履歴**: v1 2024-06-19 / v2 2024-07-18 / v3 2024-11-24(v3 は Llama 実装のバグ修正と travel スイート更新)
- **査読**: **あり** — NeurIPS 2024 Datasets and Benchmarks Track 採択(公式リポジトリの引用表記と PDF 脚注で確認)。bib は `@inproceedings`(NeurIPS 2024 D&B)に更新済み。
- **ローカル PDF**: `2406.13352v3-agentdojo.pdf`(**v3** = NeurIPS 版。PDF の arXiv スタンプで確認)
- **コード**: https://github.com/ethz-spylab/agentdojo / ドキュメント: https://agentdojo.spylab.ai/
- **要約**: 97 の現実的タスクと 629 のセキュリティテストからなる動的環境で、prompt injection の攻撃と防御を突き合わせて評価する。静的データセットでなく環境として拡張可能な点が特徴。
- **本プロジェクトとの関係**: `eval/agentdojo_live_injection.py` が本フレームワークを使ったライブ注入評価。AutoDojo・Firewalls 論文はいずれも AgentDojo を土台にしている。

確認日: 2026-08-01(arXiv abs ページ・GitHub README で確認)
