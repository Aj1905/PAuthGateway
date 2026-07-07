# Security Policy

PAuth Gateway is a security tool: a task-scoped authorization gateway that sits
between an AI agent and the SaaS/tools it can reach. We take vulnerabilities in
it seriously.

## Reporting a vulnerability

**Do not open a public issue for a security vulnerability.**

Use GitHub's private vulnerability reporting:
**Security → Report a vulnerability** on this repository. This opens a private
advisory only the maintainers can see.

Please include:

- what the gateway did vs. what it should have done,
- a minimal reproduction (prompt / plan / tool-call sequence),
- which guarantee it breaks (see the categories below).

## What is in scope

The core guarantee is **no over-authorization**: a compromised agent cannot
execute a SaaS action, or tamper with a destination/amount, beyond what the
approved plan permits. High-value reports:

- **Over-authorization (FN)** — a forced injection or fabricated operand that
  the gateway *permits* when it should deny.
- **Value leak** — a poisoned operand value reaching the agent's model context
  via feedback (agent-facing reasons must be value-free).
- **Confirmation-gate bypass** — an untrusted-derived control operand reaching a
  sink without being held for confirmation.
- **Side-channel / route bypass** that the gateway claims to cover.

## What is out of scope

Documented, accepted limitations (see `THREAT_MODEL.md`, `DESIGN_STATUS.md`,
`solution.md`):

- Injection embedded in the **user's own prompt** (the prompt is trusted).
- **Out-of-band execution** (a subprocess or direct network call that never
  reaches the gateway) in non-isolated localhost mode — this is reported by
  `protection_report()`, not prevented; the fix is an isolated runtime.
- **Over-rejection** (a legitimate action wrongly denied) — a usability issue,
  recoverable by retry, not a security failure.
- **Semantic intent faithfulness** of A1 beyond what the deterministic prechecks
  cover — an open research area; the probabilistic judge is best-effort.

## Response

We aim to acknowledge a report within a few business days and to coordinate a
fix and disclosure timeline with the reporter.
