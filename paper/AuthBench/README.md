# Do Coding Agents Understand Least-Privilege Authorization? (AuthBench)

- **bib キー**: `evolvent2026authbench`(書誌の正本は `../PAuthGateway/references.bib`)
- **著者**: PDF の署名は **Evolvent AI Research Team**(集団名義、bib もこれに準拠)。arXiv メタデータ上は個人名12名(Zheng Yan, Jingxiang Weng, Charles Chen, Dengyun Peng, Ethan Qin, Jiannan Guan, Jinhao Liu, Qiming Yu, Yixin Yuan, Fanqing Meng, Carl Che, Mengkang Hu)。引用時の著者表記は PDF 準拠(集団名義)でよいが、この食い違いは把握しておくこと。
- **初出**: arXiv:2605.14859 v1 = 2026-05-14
- **版履歴**: v1 2026-05-14 / v2 2026-05-15
- **査読**: **なし** — 企業(Evolvent AI)発の arXiv preprint。会議採択の記載なし。
- **ローカル PDF**: `2605.14859v2-authbench.pdf`(**v2**。PDF の arXiv スタンプで確認)
- **コード**: https://github.com/evolvent-ai/Authbench / 解説: https://evolvent.co/en/research/authbench
- **要約**: タスク指示と端末環境からファイル単位の read/write/execute ポリシーを推定させる「permission-boundary inference」を定式化し、120 タスクのベンチマーク AuthBench を提示。最先端モデルは必要権限の不足と不要権限の付与を同時に犯し、推論を増やしてもモデル固有の「authorization attractor」に収束するだけと報告。権限の発見(sufficiency)と監査(tightness)を分離する Sufficiency-Tightness Decomposition で、tightness 側に偏るモデルの sensitive-task 成功率を最大 15.8% 改善。
- **本プロジェクトとの関係**: `gateway/planning/sufficiency_tightness.py` と `eval/fable5_st_benchmark.py` が本論文の分解手法の実装・追試。自論文で最も密に比較される相手。

確認日: 2026-08-01(arXiv abs ページ・ローカル PDF 1ページ目で確認)
