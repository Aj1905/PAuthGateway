# Threat model and defenses

This document enumerates the threats PAuthGateway defends against, the mechanism
that defends each threat, and —— most importantly —— **which defenses are actually
built and which are design only**. It extends `architecture.md` §5 (which enumerates
the enforcement-core threat model) and adds the indirect-prompt-injection /
operand-provenance layer worked out in the design discussion.

> Honesty rule for this file: a threat is marked ✅ **Resolved (built)** **only**
> when code enforces it today. The provenance / taint / egress layers (§3, §4) are
> **design, not implementation** —— until the referenced code exists, they must not
> be cited as an operative defense.

## 0. The conceptual spine (read this first)

Two framing axes prevent the recurring confusion of collapsing distinct threats
into one phrase ("block injection," "the free operand problem").

**Axis 1 — enforcement gap vs intent-capture gap.**
- *Enforcement gap*: the agent acts outside the user-approved plan. This is an
  authorization problem. **Deterministic and resolved.**
- *Intent-capture gap*: the plan itself does not match the user's intent. This is
  a correctness problem. **No oracle exists.** It is resolved only by a human
  confirming the completed plan.

**Axis 2 — action injection vs content/data injection.**
- *Action injection*: tainted data tries to make the agent take a **new off-plan
  action**. Defended by locked plan + default-deny.
- *Content/data injection*: tainted data manipulates the **content or values that
  flow through an already-permitted action**. This is the hard residue, and the
  subject of §3–§6.

**The recurring anti-pattern (memorize it).** Every dead end we hit had the same
shape: *using attacker-controllable content as a security input*. Before trusting
any signal in a security decision, ask: **can the attacker write this?** If yes,
that defense is already broken. See §7.

## 1. Status legend

| Mark | Meaning |
|---|---|
| ✅ | **Resolved (built)** — code enforces this today |
| 🟡 | **Designed, not implemented** — the mechanism is agreed, the code does not exist yet |
| 🔶 | **Open problem** — no clean solution. An active design risk |
| ⚪ | **Out of scope (accepted)** — intentionally not defended |
| ⛔ | **Irreducible floor** — cannot be removed by any design |

## 2. Resolved — enforcement core (built)

These are enforced today by `pauth/` + `gateway/` (see `architecture.md` §4
invariants, §5 threat table). The **Why it holds** column states why each threat is
actually closed —— not just the name of the mechanism, but the property that makes
the mechanism sufficient.

| Threat | Mechanism | Why it holds | Status |
|---|---|---|---|
| Agent issues a tool call not in the plan | Default-deny on missing rule (B1) | **Closed-world enforcement.** The permitted set is exactly the rules compiled from the plan. Anything not explicitly permitted is denied. There is no path to "allow" without a matching rule, so a new call cannot slip through a gap —— the absence of a rule *is* the denial. | ✅ |
| Agent substitutes a constant operand (IBAN, amount, subject, date) | Operand match against compiled rules (B2/B3) | Operand values are fixed inside the rule at plan time. The check compares the call's operand against that fixed value. Any substitution mismatches and is denied. The agent does not write rules, so it cannot widen its own permission. | ✅ |
| Agent fabricates a derived value it never observed | Symbolic eval against envelope store; agent-reported values ignored (B3) | The gateway, not the agent, executes the tool and records the result as a signed envelope. Operand resolution reads **only** the envelope store, so a value the agent fabricated has no backing envelope → unresolved → denied. Agent-reported numbers never become inputs to the decision. | ✅ |
| Agent skips an observation and uses its symbolic result | Envelope missing → operand unresolved → deny | A derived operand is resolved only if the upstream observation actually ran (its envelope exists). Skipping the observation leaves the operand unresolved, the enforcer cannot evaluate the rule, and it denies. There is no way to "assume" a value. | ✅ |
| Agent calls a sensitive tool out of order | Guard requires upstream envelopes; missing → deny | Order is encoded as a guard dependency on upstream envelopes. Calling out of order means the required envelope does not yet exist, so the guard evaluates to false → denied. The sequence is enforced by data dependency, not by trusting the agent to order itself. | ✅ |
| Agent re-plans mid-session (e.g. on injection) | `AgentChannel` rejects a second `PromptMessage` | The plan is made exactly once and is immutable thereafter (invariant #1). A second prompt —— the vector by which injection would install a new plan —— is structurally rejected, so there is no API surface to mutate the plan after lock. | ✅ |
| **Action injection** via tool result (new off-plan action) | Plan generated from clean prompt before any tool output exists | **Temporal ordering closes it.** The plan is fully derived and locked *before* the agent reads any tool output. So injected instructions cannot influence it. An injected "do X" maps to a tool call absent from the plan → default-deny (B1). The poison arrives after the only decision it could have tainted was already made. | ✅ |
| `"ignore previous instructions"` reaches the **planner** | Planner (A1) never reads tool output | **Structural immunity, not resistance.** The planner's only input is the clean user prompt; it is never on a code path that reads email/web/tool results. The injection text cannot reach it, so there is nothing to resist —— that channel does not exist. | ✅ |
| `"ignore previous instructions"` hijacks the **executing agent** | Hijacked agent's calls are still gated against the locked plan | **The design assumes the agent is injected and does not depend on the agent resisting.** Even a fully persuaded agent can only *emit tool calls*, and every call passes B1–B4 against the immutable plan. Belief is not action: an off-plan call is denied regardless of what the agent has been persuaded to "want." | ✅ |
| Plan does not match user intent (intent-capture gap) | grill-me fills a template; the user confirms the completed plan before execution | **No oracle can judge intent, but a human can.** Correctness is not an enforceable property, so it is intentionally delegated to the only party who knows the intent —— the user who approves the concrete plan before lock. The system does not *claim* to verify intent. It makes the human the verifier. | ✅ (by human) |

## 3. Injection-within-plan layer — mostly implemented (S18–S20)

This is the residue from Axis 2: tainted data manipulates content/values inside an
*already-permitted* action. The enforcement core **does not cover** this. The
defense is provenance taint + sink classification + human escalation, and **the
core is implemented** (S18/S19/S20). What remains is only the Q-LLM and the HTTP
wire exposure of confirmation.

Defense components (current state):

| Component | What it does | Implementation |
|---|---|---|
| Source trust label | Declares which tools return untrusted data, **can default to untrusted fail-closed** | 🟢 `gateway/runtime/confirmation.py` (`SourceTrust` / `SourceTrust.fail_closed`) |
| Taint propagation | From the restricted grammar (single assignment, no loops), tracks which control operands originate from untrusted sources via **static provenance**. Taint is not dropped even through a transform (`amount*2`) | 🟢 `gateway/runtime/confirmation.py` (`static_taint_map`, S20. Static analysis, not the runtime "meet" from the design phase) |
| Sink classification | Decides the control operand (recipient/amount). Content operands are not gated (the content/control separation of S15) | 🟢 `gateway/planning/prechecks.py` (`_classify_param`) + `confirmation.py` (`control_operands`) |
| Gate B5 | `untrusted × control operand` → **hold for human confirm** (PENDING_CONFIRMATION). Fires on both the session and composite paths (S19) | 🟢 `gateway/runtime/gateway.py` (`_confirmation_gate`) |
| Confirmation round-trip | Held value + provenance to a human side channel; released on approval | 🟡 Python API implemented (`Gateway.pending_confirmations()` / `confirm()`). The HTTP wire (`confirm_request` / `confirm_response`) is not exposed |
| Quarantine LLM (Q-LLM) | Reads untrusted content **without tool access**. Output is tagged untrusted | 🔴 Not implemented |

Note: the envelope already records **which tool produced each value** (unforgeable,
HMAC-signed). The trust label is a *policy interpretation* of that origin, and lives
in policy/config, not in the envelope. So this layer only adds lookup + propagation
+ gate on top of the existing foundation, and does not change the signed envelope
schema.

Threats this addresses (**mitigated** by the confirmation gate implementation, with
known residue):

| Threat | Mechanism | Status |
|---|---|---|
| A derived operand from a tainted source flows into a **control** operand (recipient/amount) (e.g. an attacker-controlled IBAN inside an email) | Source untrusted → static provenance taint → PENDING_CONFIRMATION at the control operand → human confirm | 🟢 (S18–S20. Residue: the fan-out stage can drop provenance through observed constant folding and under-gate, S20) |
| Free-operand **content** poisoning (e.g. "include the secret in the body") to an external sink | Content operands are not gated (S15): the taint only reaches a confirmed destination and the damage is bounded. If it flows into a control operand, it is held at the layer above | 🟢 (content/control separation) |
| Q-LLM output manipulation ("output IBAN = attacker's") | Assumes a Q-LLM that reads untrusted content without tool access | 🔴 Q-LLM not implemented |

## 4. Open problems — no clean solution yet

| Problem | Why it is hard | Status |
|---|---|---|
| **Egress sink enumeration completeness** | If a harmful sink (e.g. `delete`) is misclassified as `internal-read`, injection within plan silently passes the gate. The defense is only as good as the precision of the sink inventory. **This is the current weakest point.** | 🔶 |
| Per-record trust granularity | Suite-level trust is safe but coarse (making all of Gmail untrusted → every email-derived value requires confirm). Finer trust must use **verified** metadata (DKIM/SPF-verified sender, OS path) and must never use metadata that the content claims. It needs actual verification infrastructure. | 🔶 |
| Trust configuration error | If the deployer mis-marks an untrusted source as trusted, the gate opens. Mitigated by default-untrusted, but the config is now an attack surface. | 🔶 |
| Usability cost of egress confirmation | Every `untrusted × egress` action interrupts the user. High-automation use cases feel this keenly. There is no safe, unsupervised automatic path here. | 🔶 |

## 5. Out of scope (accepted, not defended)

| Threat | Why accepted |
|---|---|
| Injection in the user's own prompt window | The input window is trusted —— the user's responsibility (`architecture.md` §5, Q11) | ⚪ |
| User disables / bypasses the hook | The user is trusted (Q0) | ⚪ |
| Side channels: filesystem, bash, child processes, env vars | The gateway can only see tool calls. A real Claude Code firewall additionally requires a Bash policy / sandbox (Q7/Q10) | ⚪ |
| Availability attack: a hijacked agent sabotages or derails the task | **It produces no harmful action** (off-plan calls are denied) —— only a lack of progress. A liveness problem, not a security breach. | ⚪ |
| Prompt-correctness beyond what the user approved | PAuth enforces the plan. It is not a correctness oracle. The user can approve a plan that does the wrong thing. | ⚪ |

## 6. The irreducible floor ⛔

After all of the above defenses, one threat remains, and **cannot be removed by any
design**:

> **The human is social-engineered at the egress confirmation** and approves a
> clearly presented malicious action.

The system's job is to shrink the problem *down to* this floor: make the
confirmation **maximally informed** (show the resolved value + the unforgeable
provenance chain —— "IBAN …, from an email from `unknown@external`, DKIM unverified")
and **minimally frequent** (only `untrusted × egress`, with plan-time pinning
shrinking the set further). Anyone who claims to remove this floor is selling a
correctness oracle that does not exist.

## 7. Explicitly rejected anti-patterns

The dead ends walked in design. Each is a concrete instance of *using
attacker-controllable content as a security input* (§0). Documented so they are not
re-proposed.

| Rejected idea | Why it breaks |
|---|---|
| An LLM judges whether an operand "aligns with intent" | The judge reads tainted data → it is itself injectable. It has no independent ground truth about the "correct" value. Fixing an injectable LLM with another injectable LLM is turtles-all-the-way-down |
| Decide a source's trust by **reading its content** | Hands the trust decision to the attacker who writes the content ("this email is from a trusted bank") |
| Trust content-claimed metadata (e.g. the `From:` header) | Forgeable. Only cryptographically **verified** provenance (DKIM/SPF) counts |
| Treat "free operand" as the unit of defense | Misses the derived-operand-from-poisoned-source case (the IBAN is a *checked* operand, but is still tainted by its source) |
| Mark a source trusted so the LLM can "understand" it | Category error: untrusted ≠ unreadable. Reading is internal and always permitted. Trust governs egress only. Marking a source trusted in order to read it opens the very hole the label exists to plug |

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

Untrusted content is freely read/understood (including by a quarantine LLM with no
tool access). What the whole system ultimately gates is only **an untrusted-sourced
value crossing the egress boundary**, and the only thing it ultimately trusts for
that decision is the human + cryptographically verifiable provenance.

## 9. Relationship to other docs

- `architecture.md` §4–§5 — the built enforcement core and its invariants.
- `design-status.md` — implementation status / bottlenecks.
- `gateway/runtime/policy.py` — marks free operands today. §3 extends this with sink
  classification and trust labels.
