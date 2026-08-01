# paper/ — 論文執筆パッケージ

- `PAuthGateway/` — 自分の論文(LaTeX 足場、`references.bib`、ビルドは `latexmk -pdf pauthgateway-template.tex`。生成物は `.latexmkrc` により `build/` に出る)
- `AUTHORING_GUIDE.md` — 執筆の制約(守り切れる主張の範囲、証拠台帳、提出前に必要な実験)
- `LLM_USE_RECORD_TEMPLATE.md` — LLM 支援の開示記録の雛形
- 各論文名フォルダ — 参照論文の PDF 置き場(下表)。各フォルダの `README.md` に
  著者・版履歴・査読状況・コードの所在などの確認済みメタデータを置く
  (PDF は git 追跡外だが README は追跡されるので、PDF が消えてもメタデータは残る)

## 参照論文 PDF の対応表

PDF は `.gitignore` の `*.pdf` により **git 追跡外(ローカル限定)**。正は
`PAuthGateway/references.bib` であり、下表の bib キーで追える。消えたら
「再取得」列のコマンドで復元する。

| bib キー | 論文 | ローカル PDF | 出典 |
|---|---|---|---|
| `sharma2026pauth` | PAuth: Precise Task-Scoped Authorization for Agents | `PAuth/2603.17170v1-pauth.pdf` | https://arxiv.org/abs/2603.17170 |
| `debenedetti2024agentdojo` | AgentDojo | `AgentDojo/2406.13352v3-agentdojo.pdf` | https://arxiv.org/abs/2406.13352 |
| `zhan2024injecagent` | InjecAgent | `InjecAgent/2024.findings-acl.624-injecagent.pdf` | https://aclanthology.org/2024.findings-acl.624/ |
| `yao2024taubench` | τ-bench | `TauBench/2406.12045v1-taubench.pdf` | https://arxiv.org/abs/2406.12045 |
| `barres2025tau2bench` | τ²-Bench(τ-bench の後継) | `Tau2Bench/2506.07982v1-tau2bench.pdf` | https://arxiv.org/abs/2506.07982 |
| `sierra2026tau3bench` | τ³-bench(単独論文なし・リポジトリが実体) | なし(release v1.0.1 で固定) | https://github.com/sierra-research/tau2-bench |
| `bhagwatkar2025firewalls` | Indirect Prompt Injections: Are Firewalls All You Need…? | `Firewalls/2510.05244v2-firewalls.pdf` | https://arxiv.org/abs/2510.05244 |
| `evolvent2026authbench` | Do Coding Agents Understand Least-Privilege Authorization? (AuthBench) | `AuthBench/2605.14859v2-authbench.pdf` | https://arxiv.org/abs/2605.14859 |
| `ma2026autodojo` | AutoDojo | `AutoDojo/2606.15057v2-autodojo.pdf` | https://arxiv.org/abs/2606.15057 |

## 再取得(全件)

版は URL 側でも固定してある(`v3` 等)。素の ID で落とすと常に最新版になり、
引用中の数値と食い違う恐れがあるので、URL の版指定を外さないこと。

```bash
cd paper && mkdir -p PAuth AgentDojo InjecAgent TauBench Tau2Bench Firewalls AuthBench AutoDojo && curl -sL -o PAuth/2603.17170v1-pauth.pdf https://arxiv.org/pdf/2603.17170v1 && curl -sL -o AgentDojo/2406.13352v3-agentdojo.pdf https://arxiv.org/pdf/2406.13352v3 && curl -sL -o InjecAgent/2024.findings-acl.624-injecagent.pdf https://aclanthology.org/2024.findings-acl.624.pdf && curl -sL -o TauBench/2406.12045v1-taubench.pdf https://arxiv.org/pdf/2406.12045v1 && curl -sL -o Tau2Bench/2506.07982v1-tau2bench.pdf https://arxiv.org/pdf/2506.07982v1 && curl -sL -o Firewalls/2510.05244v2-firewalls.pdf https://arxiv.org/pdf/2510.05244v2 && curl -sL -o AuthBench/2605.14859v2-authbench.pdf https://arxiv.org/pdf/2605.14859v2 && curl -sL -o AutoDojo/2606.15057v2-autodojo.pdf https://arxiv.org/pdf/2606.15057v2
```

新しい参照論文を足すときの規則: (1) `references.bib` にエントリを追加、
(2) `paper/<論文名>/<arXiv ID>v<版>-<略称>.pdf` に置く(版をファイル名と
取得 URL の両方に含める)、(3) この表に1行足す、
(4) `paper/<論文名>/README.md` を書く(著者・版履歴・査読状況・コード・確認日。
事実は arXiv abs ページ等で確認してから書き、推測は推測と明記する)。
bib に無い PDF を置かない。

論文が存在しないフレームワークを参照する場合は、Git リポジトリを引用物として
扱う: bib には `@misc`(リリースタグまたはコミットで版固定、アクセス日を note に)、
フォルダには PDF なしで README のみを置き、固定した release/commit を明記する。
例: `Tau3Bench/`(bib キー `sierra2026tau3bench`)。

検討したが現行計画では使わないと判断した論文・フレームワークは、ここには入れず
`../docs/FUTURE_RESEARCH.md`(追加研究の候補)に判断理由付きで記録する。
