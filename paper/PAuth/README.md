# PAuth: Precise Task-Scoped Authorization for Agents

- **bib キー**: `sharma2026pauth`(書誌の正本は `../PAuthGateway/references.bib`)
- **著者**: Reshabh K. Sharma(University of Washington), Linxi Jiang・Zhiqiang Lin(The Ohio State University), Shuo Chen(Microsoft Research)— PDF 1ページ目で確認
- **初出**: arXiv:2603.17170 v1 = 2026-03-17
- **版履歴**: v1 のみ(2026-08-01 時点)
- **査読**: **なし** — arXiv preprint のまま。会議採択の記載なし。ライセンスは CC BY 4.0。
- **ローカル PDF**: `2603.17170v1-pauth.pdf`(**v1**。PDF の arXiv スタンプで確認)
- **コード**: arXiv abs ページにリンクなし(2026-08-01 時点で公開コード未確認)
- **要約**: 正式には Precise Task-Scoped **Implicit** Authorization。OAuth 型の operator-scoped な広い権限委譲では過剰権限が不可避と論じ、自然言語タスクの提出がその忠実な遂行に必要な具体的操作だけを暗黙に認可するモデルを提案。サーバー側で強制するために、各サービスが期待する呼び出しの記号的仕様「NL slices」と、オペランド値を来歴に束縛する「envelopes」を導入。AgentDojo 上で試作し、良性タスクは追加権限なしで完遂、攻撃タスクでは注入操作を欠落権限として検出したと報告。
- **本プロジェクトとの関係**: 名前の通り本プロジェクト(PAuthGateway)の直接の出発点。「認可の粒度」を提案する側で、本プロジェクトはその実行系・ゲートウェイ側。差分を明確に書くことが自論文の新規性主張の中心になる。

確認日: 2026-08-01(arXiv abs ページで確認)
