# Indirect Prompt Injections: Are Firewalls All You Need, or Stronger Benchmarks?

- **bib キー**: `bhagwatkar2025firewalls`(書誌の正本は `../PAuthGateway/references.bib`)
- **著者**: Rishika Bhagwatkar, Kevin Kasa, Abhay Puri, Gabriel Huang, Irina Rish, Graham W. Taylor, Krishnamurthy Dj Dvijotham, Alexandre Lacoste(ServiceNow / Mila / Université de Montréal / University of Guelph / Vector Institute — PDF 1ページ目で確認)
- **初出**: arXiv:2510.05244 v1 = 2025-10-06
- **版履歴**: v1 2025-10-06 / v2 2026-03-23
- **査読**: **なし** — 2026-08-01 時点で arXiv preprint のまま。会議採択の記載なし。
- **ローカル PDF**: `2510.05244v2-firewalls.pdf`(**v2**。PDF の arXiv スタンプで確認)。⚠️ v1→v2 で主張・数値が動いている可能性があるため、引用は v2 で固定。
- **コード/サイト**: プロジェクトページ https://firewall-defenses.github.io (PDF 1ページ目に記載)
- **要約**: Tool-Input Firewall(Minimizer)と Tool-Output Firewall(Sanitizer)の2枚をエージェントとツールの境界に置くだけで、AgentDojo・Agent Security Bench・InjecAgent・τ-Bench の4公開ベンチ全てで高い実用性を保ったまま完全防御を達成したと主張。同時に、これらベンチの成功指標の欠陥・実装バグ・攻撃の弱さを指摘し、「防御が強い」のではなく「ベンチが甘い」可能性を突きつける。3段構え(標準攻撃→二次攻撃→適応攻撃)の評価手順を提案。
- **本プロジェクトとの関係**: ゲートウェイ型防御(本プロジェクトと同じ配置)の有効性を支持する側の証拠。同時に、AgentDojo だけで防御を主張する危うさの根拠としても引ける。

確認日: 2026-08-01(arXiv abs ページで確認)
