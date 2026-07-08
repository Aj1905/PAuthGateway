# Business operations

This document records the OSS and commercial operating model intended for the
gateway. It is kept separate from the technical architecture so that product
packaging does not distort the security boundary.

## Positioning

PAuth Gateway should be positioned as an open-source, local-first safety gateway
for AI agents.

The core promise is this:

```text
Run an agent-adjacent gateway that captures prompt/tool events and enforces
task-scoped authorization before SaaS/API actions execute.
```

The project should win trust by making the core enforcement path inspectable and
self-hostable, and by being useful even without any paid service.

## OSS Core

The following capabilities should remain free and open source, because they are
necessary for trust, verification, and adoption:

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

The free version must not be deliberately made less safe. If users must pay to
get a real protection path, a security-oriented OSS project loses its
credibility.

## Commercial Layer

The paid offering should focus on operational burden, enterprise assurance, and
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

The commercial value is not a "safe version". The commercial value is making
safe deployment, monitoring, integration, and governance less painful for
organizations.

## Pricing Boundary Heuristic

When evaluating a future feature, use this rule:

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
| OSS core | Must be free for trust, verification, and ecosystem growth. |
| OSS basic + paid advanced | The basic local version is free; team/enterprise controls are paid. |
| Paid | Primarily operational, compliance, and support value, or custom-integration value. |
| Do not paywall | Paywalling it would undermine credibility or the security claim. |
| Do not build yet | Interesting, but distracts from validating prompt capture and enforcement. |

## Examples

| Feature | Default classification | Reason |
|---|---|---|
| Local gateway daemon | OSS core | This is the product's trust anchor. |
| Core validator | Do not paywall | Users must be able to inspect how a plan is accepted or rejected. |
| Basic Claude Code adapter | OSS core | The first adapter must prove the architecture. |
| Basic Codex adapter | OSS core | Agent coverage drives adoption. |
| Adapter contract | OSS core | Needed for third parties to build integrations. |
| Local audit log | OSS core | Users need to inspect decisions. |
| Centralized audit dashboard | Paid | Team-operations and retention burden. |
| SSO/RBAC | Paid | Enterprise governance feature. |
| Compliance export | Paid | Organization-specific reporting burden. |
| Custom SaaS adapter | Paid | Consulting / integration effort. |
| Certified adapter program | Paid | Ongoing compatibility and support cost. |
| Managed cloud gateway | Do not build yet | It would take on custody of sensitive prompts, args, credentials, and logs. |

## Strategic Risks

### Paywalling Trust

If enforcement, validation, audit visibility, and the adapter contract are
closed, the project looks like a proprietary security product that uses OSS
merely as marketing. That is a weak positioning.

### Free Version As Demo Only

If the OSS version cannot protect a real local workflow, there is no adoption. It
must be useful to individual developers and researchers.

### Enterprise Features Too Early

Building a dashboard, RBAC, compliance export, and managed services before the
local agent-adjacent gateway has proven its value is too early. The bottleneck is
still prompt capture, tool-call enforcement, bypass control, and A1 faithfulness.

### Managed Cloud Too Early

A managed cloud gateway may handle user prompts, tool arguments, credentials, and
audit logs, so it immediately creates trust, legal, and security burdens. It
should be started only after the OSS / self-host model has become trustworthy.

## Operating Principles

1. Keep safety-critical paths open and inspectable.
2. Charge for operational complexity, not for basic safety.
3. Make local-first self-hosting the default trust story.
4. Fund custom adapters and enterprise deployment work with paid consulting.
5. Classify every new feature before implementing it, so the packaging does not
   drift.

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

This boundary should be revisited only after the local OSS gateway has protected
real workflows beyond the mock suite.
