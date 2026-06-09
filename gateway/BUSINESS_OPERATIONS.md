# Business operations

This document records the intended OSS and commercial operating model for the
gateway. It is separate from the technical architecture so product packaging
does not distort security boundaries.

## Positioning

PAuth Gateway should be positioned as an open-source, local-first safety
gateway for AI agents.

The core promise is:

```text
Run an agent-adjacent gateway that captures prompt/tool events and enforces
task-scoped authorization before SaaS/API actions execute.
```

The project should earn trust by making the core enforcement path inspectable,
self-hostable, and useful without a paid service.

## OSS Core

These capabilities should remain free and open source because they are required
for trust, verification, and adoption:

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

Do not make the free version intentionally unsafe. A security OSS project loses
credibility if users must pay to get the real protection path.

## Commercial Layer

Paid offerings should focus on operational burden, enterprise assurance, and
integration work:

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

The commercial value is not "the safe version". The commercial value is making
safe deployment, monitoring, integration, and governance less painful for
organizations.

## Pricing Boundary Heuristic

Use this rule when evaluating future features:

```text
If the feature is necessary to understand, verify, or run the core safety
boundary, keep it OSS.

If the feature reduces organizational deployment, compliance, monitoring, or
maintenance burden, it can be commercial.

If paywalling the feature would make the free gateway materially less safe or
less auditable, do not paywall it.
```

Feature classification:

| Classification | Meaning |
|---|---|
| OSS core | Must be free for trust, verification, or ecosystem growth. |
| OSS basic + paid advanced | Basic local version free; team/enterprise controls paid. |
| Paid | Mostly operational, compliance, support, or custom integration value. |
| Do not paywall | Paywalling would damage credibility or security claims. |
| Do not build yet | Interesting, but distracts from prompt capture and enforcement validation. |

## Examples

| Feature | Default classification | Reason |
|---|---|---|
| Local gateway daemon | OSS core | This is the product's trust anchor. |
| Core validator | Do not paywall | Users must inspect how plans are accepted or rejected. |
| Basic Claude Code adapter | OSS core | First adapter must prove the architecture. |
| Basic Codex adapter | OSS core | Agent coverage drives adoption. |
| Adapter contract | OSS core | Third parties need it to build integrations. |
| Local audit log | OSS core | Users need to inspect decisions. |
| Centralized audit dashboard | Paid | Team operations and retention burden. |
| SSO/RBAC | Paid | Enterprise governance feature. |
| Compliance export | Paid | Organization-specific reporting burden. |
| Custom SaaS adapter | Paid | Consulting/integration labor. |
| Certified adapter program | Paid | Ongoing compatibility and support cost. |
| Managed cloud gateway | Do not build yet | Requires taking custody of sensitive prompts, args, credentials, and logs. |

## Strategic Risks

### Paywalling Trust

If enforcement, validation, audit visibility, or adapter contracts are closed,
the project will look like a proprietary security product using OSS only as
marketing. That is weak positioning.

### Free Version As Demo Only

If the OSS version cannot protect a real local workflow, it will not create
adoption. It must be useful for individual developers and researchers.

### Enterprise Features Too Early

Building dashboards, RBAC, compliance exports, and managed services before the
local agent-adjacent gateway proves value is premature. The bottleneck is still
prompt capture, tool-call enforcement, bypass control, and A1 faithfulness.

### Managed Cloud Too Early

A managed cloud gateway creates immediate trust, legal, and security burdens
because it may handle user prompts, tool arguments, credentials, and audit
logs. It should come only after the OSS/self-hosted model is credible.

## Operating Principles

1. Keep the safety-critical path open and inspectable.
2. Charge for operational complexity, not for basic safety.
3. Make local-first self-hosting the default trust story.
4. Let paid consulting fund custom adapters and enterprise deployment work.
5. Classify every new feature before implementation so packaging does not drift.

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

This boundary should be revisited only after the local OSS gateway protects
real workflows beyond mock suites.
