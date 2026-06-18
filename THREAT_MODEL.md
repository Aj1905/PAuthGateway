# Threat model and defenses

This document enumerates the threats PAuthGateway defends against, the
mechanism that defends each, and — crucially — **which defenses are
actually built versus only designed**. It extends `architecture.md` §5
(which lists the enforcement-core threat model) with the
indirect-prompt-injection / operand-provenance layer worked out in
design discussion.

> Honesty rule for this file: a threat is only marked ✅ **Resolved
> (built)** if code enforces it today. The provenance / taint / egress
> layer (§3, §4) is **design, not implementation** — do not cite it as a
> live defense until the referenced code exists.

## 0. The conceptual spine (read this first)

Two framing axes prevent the recurring confusion of collapsing distinct
threats into one word ("blocks injection", "free operand problem").

**Axis 1 — enforcement gap vs intent-capture gap.**
- *Enforcement gap*: the agent acts outside the user-approved plan. This
  is an authorization problem. It is **deterministic and solved**.
- *Intent-capture gap*: the plan itself does not match what the user
  meant. This is a correctness problem. **No oracle exists**; it is
  resolved only by the human confirming the completed plan.

**Axis 2 — action injection vs content/data injection.**
- *Action injection*: poisoned data tries to make the agent take a **new,
  off-plan action**. Defended by the locked plan + default-deny.
- *Content/data injection*: poisoned data manipulates the **content or
  value flowing through an already-permitted action**. This is the hard
  residual and the subject of §3–§6.

**The recurring anti-pattern (memorize this).** Every dead-end we hit had
the same shape: *using attacker-controllable content as a security
input*. Before trusting any signal for a security decision, ask: **can
the attacker write this?** If yes, the defense is already broken. See §7.

## 1. Status legend

| Mark | Meaning |
|---|---|
| ✅ | **Resolved (built)** — code enforces this today |
| 🟡 | **Designed, not implemented** — mechanism agreed, no code yet |
| 🔶 | **Open problem** — no clean solution; active design risk |
| ⚪ | **Out of scope (accepted)** — deliberately not defended |
| ⛔ | **Irreducible floor** — cannot be eliminated by any design |

## 2. Resolved — enforcement core (built)

These are enforced by `pauth/` + `gateway/` today (see `architecture.md`
§4 invariants, §5 threat table). The **Why it holds** column states the
reason each threat is actually closed — not just the mechanism's name,
but the property that makes the mechanism sufficient.

| Threat | Mechanism | Why it holds | Status |
|---|---|---|---|
| Agent issues a tool call not in the plan | Default-deny on missing rule (B1) | **Closed-world enforcement.** The permitted set is exactly the rules compiled from the plan; anything not explicitly permitted is denied. There is no path to "allow" without a matching rule, so a novel call cannot slip through a gap — the absence of a rule *is* the denial. | ✅ |
| Agent substitutes a constant operand (IBAN, amount, subject, date) | Operand match against compiled rules (B2/B3) | The operand value is fixed in the rule at plan time. The check compares the call's operand to that pinned value; any substitution mismatches and is denied. The agent cannot widen its own permission because it does not author the rule. | ✅ |
| Agent fabricates a derived value it never observed | Symbolic eval against envelope store; agent-reported values ignored (B3) | The gateway, not the agent, executes tools and records results as signed envelopes. Operand resolution reads **only** the envelope store, so a value the agent invents has no envelope backing it → unresolved → denied. Agent-reported numbers are never an input to the decision. | ✅ |
| Agent skips an observation and uses its symbolic result | Envelope missing → operand unresolved → deny | A derived operand resolves only if the upstream observation actually ran (its envelope exists). Skipping the observation leaves the operand unresolved; the enforcer cannot evaluate the rule and denies. There is no way to "assume" the value. | ✅ |
| Agent calls a sensitive tool out of order | Guard requires upstream envelopes; missing → deny | Ordering is encoded as guard dependencies on upstream envelopes. Calling out of order means the required envelopes do not yet exist, so the guard evaluates false → deny. Sequence is enforced by data dependency, not by trusting the agent to order itself. | ✅ |
| Agent re-plans mid-session (e.g. on injection) | `AgentChannel` rejects a second `PromptMessage` | The plan is created exactly once and is immutable thereafter (invariant #1). A second prompt — the vector by which injection would install a new plan — is structurally rejected, so there is no API surface to mutate the plan after lock. | ✅ |
| **Action injection** via tool result (new off-plan action) | Plan generated from clean prompt before any tool output exists | **Temporal ordering closes it.** The plan is fully derived and locked *before* the agent reads any tool output, so no injected instruction can have influenced it. An injected "do X" maps to a tool call absent from the plan → default-deny (B1). The poison arrives after the only decision it could have corrupted was already made. | ✅ |
| `"ignore previous instructions"` reaches the **planner** | Planner (A1) never reads tool output | **Structural immunity, not resistance.** The planner's only input is the clean user prompt; it is never on a code path that reads email/web/tool results. The injection text cannot reach it at all, so there is nothing to resist — the channel does not exist. | ✅ |
| `"ignore previous instructions"` hijacks the **executing agent** | Hijacked agent's calls are still gated against the locked plan | **The design assumes the agent is injected and does not depend on it resisting.** Even a fully convinced agent can only *emit tool calls*, and every call passes through B1–B4 against the immutable plan. Belief is not action: off-plan calls are denied regardless of what the agent was persuaded to "want." | ✅ |
| Plan does not match user intent (intent-capture gap) | grill-me fills a template; the user confirms the completed plan before execution | **No oracle can decide intent; the human can.** Correctness is not an enforceable property, so it is deliberately moved to the one party who knows the intent — the user, who approves the concrete plan before it locks. The system does not *claim* to verify intent; it makes the human the verifier. | ✅ (by human) |

## 3. Designed, not implemented — injection-within-plan layer

This is the residual from Axis 2: poisoned data manipulating content/values
inside an *already-permitted* action. The enforcement core does **not**
cover it. The agreed defense is provenance taint + sink classification +
human escalation. **None of this is built yet.**

Defense components (target files):

| Component | What it does | Target |
|---|---|---|
| Source trust label | Each source suite declares `trust: trusted \| untrusted`, **default untrusted** | `gateway/registry.py`, `gateway/config.py` |
| Taint propagation | Derived operands inherit the **meet** (most-untrusted) of their inputs | `pauth/evaluator.py` |
| Sink classification | Each `(tool[, param])` tagged `internal-read` vs `egress/irreversible` | `gateway/policy.py` |
| Gate B5 | `untrusted × egress` → **escalate to human confirm** (new PERMIT / DENY / **CONFIRM** outcome) | `pauth/enforcer.py` |
| Confirmation round-trip | New wire messages `confirm_request` / `confirm_response`, showing resolved value + provenance chain | `gateway/agent_channel.py`, `gateway/http_server.py` |
| Quarantine LLM (Q-LLM) | Reads untrusted content with **no tool access**; output tagged untrusted; never decides permit/deny | new |

Note: the envelope already records **which tool produced each value**
(unforgeable, HMAC-signed). The trust label is a *policy interpretation*
of that origin and lives in policy/config, **not** in the envelope. So
this layer adds a lookup + propagation + gate on top of an existing
foundation; it does not modify the signed envelope schema.

Threats this addresses — **currently UNMITIGATED until the above ships**:

| Threat | Mechanism (when built) | Status |
|---|---|---|
| Free-operand content poisoning (e.g. "include all secrets in the message body") flowing to an external sink | Taint on free operand → egress gate → human confirm | 🟡 |
| Derived operand from a poisoned source (e.g. attacker-controlled invoice IBAN in an email) | Source untrusted → taint propagates → egress gate → human confirm | 🟡 |
| Q-LLM output manipulation ("output IBAN = attacker's") | No tool access → cannot escalate to action; tagged untrusted → hits egress gate | 🟡 |

## 4. Open problems — no clean solution yet

| Problem | Why it is hard | Status |
|---|---|---|
| **Egress sink enumeration completeness** | If a harmful sink (e.g. `delete`) is mis-classified as `internal-read`, injection within plan passes the gate silently. The defense is only as good as the sink inventory. **This is the current weakest point.** | 🔶 |
| Per-record trust granularity | Suite-level trust is safe but coarse (all Gmail untrusted → every email-derived value needs confirm). Finer trust must use **verified** metadata (DKIM/SPF-validated sender, OS path), never content-claimed metadata. Requires real verification infra. | 🔶 |
| Trust configuration error | A deployer mis-marking an untrusted source as trusted opens the gate. Mitigated by default-untrusted, but config is now an attack surface. | 🔶 |
| Usability cost of egress confirmation | Every `untrusted × egress` action interrupts the user. High-automation use cases feel this. No automated path is both safe and unsupervised here. | 🔶 |

## 5. Out of scope (accepted, not defended)

| Threat | Why accepted |
|---|---|
| Injection in the user's own prompt window | Input window is trusted — the user's responsibility (`architecture.md` §5, Q11) | ⚪ |
| User disables / bypasses the hook | User is trusted (Q0) | ⚪ |
| Side channels: filesystem, bash, child processes, env vars | Gateway sees only tool calls; a real Claude Code firewall additionally needs Bash policy / sandbox (Q7/Q10) | ⚪ |
| Availability attack: a hijacked agent sabotages or derails the task | Produces **no harmful action** (off-plan calls are denied) — only lack of progress. A liveness problem, not a security breach. | ⚪ |
| Prompt-correctness beyond what the user approved | PAuth enforces the plan; it is not a correctness oracle. The user may approve a plan that does the wrong thing. | ⚪ |

## 6. The irreducible floor ⛔

After every defense above, one threat remains and **cannot be removed by
any design**:

> **The human is socially engineered at the egress confirmation** and
> approves a malicious action that was presented clearly.

The system's job is to reduce the problem *to* this floor: make the
confirmation **maximally informed** (show the resolved value + an
unforgeable provenance chain — "IBAN …, sourced from an email from
`unknown@external`, DKIM unverified") and **minimally frequent** (only
`untrusted × egress`, with plan-time pinning shrinking the set further).
Anyone claiming to eliminate this floor is selling the correctness oracle
that does not exist.

## 7. Explicitly rejected anti-patterns

Dead-ends walked through in design. Each is a concrete instance of *using
attacker-controllable content as a security input* (§0). Documented so
they are not re-proposed.

| Rejected idea | Why it breaks |
|---|---|
| An LLM judges whether an operand "aligns with intent" | The judge reads the poisoned data → is itself injectable; has no independent ground truth for the "right" value; fixing an injectable LLM with another injectable LLM is turtles-all-the-way-down |
| Decide a source's trust by **reading its content** | Hands the trust decision to the attacker, who writes the content ("this email is from your trusted bank") |
| Trust content-claimed metadata (e.g. the `From:` header) | Forgeable; only cryptographically **verified** provenance (DKIM/SPF) counts |
| Treat "free operand" as the unit of defense | Misses the derived-operand-from-poisoned-source case (the IBAN is a *checked* operand, yet still poisoned at its source) |
| Mark a source trusted so the LLM can "understand" it | Category error: untrusted ≠ unreadable. Reading is internal and always allowed; trust only governs egress. Marking it trusted to read it opens the very hole the label exists to close. |

## 8. End-to-end defense flow

```
User prompt
   │  grill-me fills template; operands classified pinned (USER) vs derived (read-time)
   ▼
HUMAN CONFIRM the completed plan   ← intent-capture gap closed here (§2)
   │  plan locked; never re-planned
   ▼
─── execution; per tool call ───
   ▼
B1–B4  plan enforcement (built, §2)         ← action injection blocked here
   │  permitted by plan
   ▼
B5  taint × sink  (designed, §3)            ← content/data injection handled here
   │   trusted?            → pass
   │   untrusted × internal → pass (no egress harm; reading/understanding never blocked)
   │   untrusted × egress   → CONFIRM
   ▼
HUMAN CONFIRM the egress value + provenance chain   ← irreducible floor (§6)
   │
   ▼
suite.runner executes · envelope records signed observation (built, §2)
```

Untrusted content is read/understood freely (including by a quarantine
LLM with no tool access). The only thing the whole system ultimately
gates is **an untrusted-sourced value crossing an egress boundary**, and
the only thing it ultimately trusts for that decision is the human plus
cryptographically verifiable provenance.

## 9. Relationship to other docs

- `architecture.md` §4–§5 — the built enforcement core and its invariants.
- `gateway/DESIGN_STATUS.md` — implementation status / bottlenecks.
- `gateway/policy.py` — today marks free operands; §3 extends it with sink
  classification and trust labels.
- `grill.md` — decision history (Q-numbered).
