# Business operations

このドキュメントは、gateway について意図している OSS および商用の運用モデルを
記録する。プロダクトのパッケージングがセキュリティ境界を歪めないよう、技術
アーキテクチャとは分離してある。

## Positioning

PAuth Gateway は、AI agent 向けのオープンソースで local-first な safety gateway と
して位置づけるべきだ。

中核となる約束（promise）はこうだ:

```text
Run an agent-adjacent gateway that captures prompt/tool events and enforces
task-scoped authorization before SaaS/API actions execute.
```

このプロジェクトは、中核となる enforcement 経路を inspectable（検査可能）・
self-hostable（自己ホスト可能）にし、有償サービスなしでも有用であることによって、
信頼を勝ち取るべきだ。

## OSS Core

以下の機能は、信頼・検証・採用のために必要なので、無料かつオープンソースの
ままとすべきだ:

- core PAuth enforcement;
- local gateway daemon;
- basic agent ingress adapters;
- basic MCP/OpenAPI/SaaS adapter framework;
- planner strategy framework;
- deterministic and basic LLM planner paths;
- restricted-code validator;
- local audit log;
- basic health checks;
- basic bypass/protection-level reporting;
- Gateway Integration Contract for adapter authors;
- self-host setup documentation.

無料版を意図的に安全でなくしてはならない。本当の保護経路を得るためにユーザーが
支払わねばならないなら、セキュリティ系 OSS プロジェクトは信用を失う。

## Commercial Layer

有償提供は、運用負担・エンタープライズ保証・統合作業に焦点を当てるべきだ:

- team dashboard;
- centralized audit storage;
- long-term retention;
- compliance reports and exports;
- SSO, RBAC, and organization policy distribution;
- policy approval workflows;
- managed API-spec change notification;
- adapter certification and compatibility testing;
- custom agent adapters;
- custom SaaS adapters;
- enterprise deployment templates;
- security review and threat modeling;
- implementation consulting;
- paid support and maintenance.

商用としての価値は「安全な版」ではない。商用としての価値は、組織にとって安全な
デプロイ・監視・統合・ガバナンスをより苦痛でなくすることだ。

## Pricing Boundary Heuristic

将来の機能を評価するときは、このルールを用いる:

```text
If the feature is necessary to understand, verify, or run the core safety
boundary, keep it OSS.

If the feature reduces organizational deployment, compliance, monitoring, or
maintenance burden, it can be commercial.

If paywalling the feature would make the free gateway materially less safe or
less auditable, do not paywall it.
```

機能の分類:

| 分類 | 意味 |
|---|---|
| OSS core | 信頼・検証・エコシステム成長のために無料でなければならない。 |
| OSS basic + paid advanced | 基本のローカル版は無料。team/enterprise 向けの制御は有償。 |
| Paid | 主に運用・compliance・サポート、または custom 統合の価値。 |
| Do not paywall | paywall すれば信用やセキュリティの主張を損なう。 |
| Do not build yet | 興味深いが、prompt capture と enforcement の検証から注意を逸らす。 |

## Examples

| Feature | Default classification | Reason |
|---|---|---|
| Local gateway daemon | OSS core | これはプロダクトの信頼の拠り所（trust anchor）。 |
| Core validator | Do not paywall | ユーザーは plan がどう受理・却下されるかを検査できねばならない。 |
| Basic Claude Code adapter | OSS core | 最初の adapter はアーキテクチャを証明せねばならない。 |
| Basic Codex adapter | OSS core | agent のカバレッジが採用を牽引する。 |
| Adapter contract | OSS core | サードパーティが統合を構築するのに必要。 |
| Local audit log | OSS core | ユーザーは decision を検査する必要がある。 |
| Centralized audit dashboard | Paid | team 運用と retention の負担。 |
| SSO/RBAC | Paid | エンタープライズのガバナンス機能。 |
| Compliance export | Paid | 組織固有のレポーティング負担。 |
| Custom SaaS adapter | Paid | コンサル / 統合の労力。 |
| Certified adapter program | Paid | 継続的な互換性とサポートのコスト。 |
| Managed cloud gateway | Do not build yet | 機微な prompt・args・credential・log の保管責任（custody）を負うことになる。 |

## Strategic Risks

### Paywalling Trust

enforcement・validation・監査の可視性・adapter contract が閉じられていると、
このプロジェクトは OSS を単なるマーケティングとして使うプロプライエタリな
セキュリティ製品に見えてしまう。それは弱いポジショニングだ。

### Free Version As Demo Only

OSS 版が実際のローカルワークフローを保護できないなら、採用は生まれない。個人開発者や
研究者にとって有用でなければならない。

### Enterprise Features Too Early

ローカルの agent-adjacent gateway が価値を証明する前に、dashboard・RBAC・compliance
export・managed サービスを構築するのは早すぎる。ボトルネックは依然として prompt
capture、tool-call enforcement、bypass 制御、そして A1 の faithfulness だ。

### Managed Cloud Too Early

managed cloud gateway は、user prompt・tool argument・credential・audit log を扱い
うるため、即座に信頼・法務・セキュリティ上の負担を生む。OSS / 自己ホストモデルが
信用できるものになってから初めて着手すべきだ。

## Operating Principles

1. safety-critical な経路は開かれた検査可能なままに保つ。
2. 運用上の複雑さに課金し、基本的な安全性には課金しない。
3. local-first な自己ホストをデフォルトの信頼ストーリーにする。
4. 有償のコンサルティングで custom adapter とエンタープライズのデプロイ作業を賄う。
5. パッケージングがぶれないよう、新機能はすべて実装前に分類する。

## Current Packaging Direction

```text
Free OSS:
  local gateway
  core enforcement
  basic adapters
  local audit
  planner framework
  adapter contract

Paid later:
  team operations
  centralized audit
  SSO/RBAC
  compliance reporting
  certified adapters
  custom integrations
  deployment consulting
```

この境界は、ローカルの OSS gateway が mock suite を超えた実ワークフローを保護した
後にのみ見直すべきだ。
