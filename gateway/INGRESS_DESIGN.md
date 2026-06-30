# Ingress design: SDK-direct first, interception later

This memo records how the gateway attaches to an agent, and the decision to make
**SDK / direct integration the first ingress (beachhead)** while keeping the
**interception ingress (proxy / hooks) as a slot behind the same contract**,
built later.

It follows the `DESIGN_STATUS.md` discipline: confirmed decisions and open
questions are kept separate so the design does not look more settled than it is.

Cross-references: `architecture.md` §1.1/§1.2 (ingress boundary — uses "ingress"
at the *adapter* level; see its "Terminology note" pointing back to the
directional model here; the leg model lands in architecture.md only once
interception is implemented), `plan.md` issue B5 (Bash escape hatch),
`DESIGN_STATUS.md` bottleneck #2 (prompt capture is the main product risk),
`BUSINESS_STRATEGY.md` §3.1 (target segment decision).

## Core principle: ingress mode is a function of who owns the agent

| Who owns the agent | Ingress mode | Why |
|---|---|---|
| The customer builds it (self-developed agent) | **SDK / direct integration** | The customer owns the code, so they call the gateway (pauth core) directly. No interception needed. |
| A third party (unmodified Claude Code, Codex, ...) | **Interception** (inference proxy / hooks) | The code cannot be changed, so prompt and tool events must be captured from outside. |

Both ingress modes normalize into the **same** `PromptMessage` /
`ToolCallMessage` contract (`gateway/ingress/agent_channel.py`) and feed the **same**
deterministic core (`pauth/`). Only the ingress adapter differs. This is exactly
what the loose-coupling boundary in `architecture.md` was built for.

But note: **"ingress" is used at two levels in this memo** — the *adapter* (SDK
vs interception, above) and the *wire-level direction* of each capture/enforcement
tap. Capture and enforcement do **not** sit on the same leg of the round trip.
See "Directional model" below before reading Mode 2.

## Directional model: "ingress" ≠ a single direction (往路/復路 × ingress/egress)

The agent↔provider exchange is a **round trip**, so relative to the gateway there
are four legs, not one. Conflating them hides the fact that the gateway can only
*observe* on some legs and only *enforce* on another.

```text
          往路ingress              往路egress
agent ──────────────────▶ gateway ──────────────────▶ provider
      ◀──────────────────         ◀──────────────────
          復路egress              復路ingress
```

| Leg | Wire direction | What flows | Gateway's job |
|---|---|---|---|
| **往路ingress** | agent → gateway | user prompt (request in) | **observe** prompt → `PromptMessage`; plan-once (A1–A3) |
| **往路egress** | gateway → provider | user prompt (request out) | relay; optional prompt redaction before it leaves |
| **復路ingress** | provider → gateway | model's `tool_use` (response in) | **observe** tool calls → `ToolCallMessage` |
| **復路egress** | gateway → agent | response (response out) | **enforce** — rewrite/block a denied `tool_use` before the agent sees it (B1–B4) |

Two consequences fall straight out of this:

1. **Observation lives on the ingress legs; enforcement lives on 復路egress.**
   Capturing the prompt (往路ingress) and capturing tool calls (復路ingress) are
   read-only taps. Actually *stopping* a tool call requires acting on
   **復路egress** — a read-write tap. This is the wire-level statement of
   "capture is not enforcement" (Mode 2 below).
2. **The two contracts map to the two ingress legs.** `PromptMessage` = 往路ingress;
   `ToolCallMessage` = 復路ingress. The core never touches egress directly — it
   returns a decision that the **復路egress** leg applies.

The **tool-execution channel** (agent ↔ MCP / external tool) is a *second* round
trip with its own four legs. The tool proxy (B, below) acts on **its 往路**
(agent → tool request), not on the inference round trip at all.

How each mode occupies these legs:

| | 往路ingress | 往路egress | 復路ingress | 復路egress |
|---|---|---|---|---|
| **Mode 1 SDK** | `submit_user_prompt` (out-of-band call) | — (agent calls provider itself) | `handle_tool_call` (out-of-band call) | decision = function return value; **the agent's own code applies it** |
| **Mode 2 inference proxy** | proxy reads request | proxy relays | proxy reads response | proxy rewrites/blocks — path (A) |

In **Mode 1 the gateway is not inline**: it is a callee beside the agent, so
往路egress does not exist for it and "enforcement" is only the boolean the
customer's code agrees to honor. In **Mode 2 the gateway is inline**, so all four
legs are real and **復路egress is where the (fragile) response rewriting must
happen** — which is exactly why it can desync the agent's state.

## Decision

- **Beachhead = Mode 1 (SDK / direct), self-built-agent / ToB segment.** Build
  now.
- **Mode 2 (interception) is sequenced after.** Keep the ingress boundary open
  so it can attach behind the same contract, but **do not implement the
  interception adapter yet.**
- **ToC is not a paying segment.** No subscription-based payment / billing path
  for consumers. See `BUSINESS_STRATEGY.md`.

Build discipline (three layers, do not collapse them):

| Layer | Build now? | Note |
|---|---|---|
| Shared core (`pauth/`, enforcer, envelope, `AgentChannel` contract) | **Yes** | Serves both modes. The foundation. |
| Ingress boundary (clean, contract-stable seam) | **Already exists** | Keep it clean so Mode 2 can attach later. |
| Mode 1 SDK ingress | **Yes** | The beachhead. First customers use this. |
| Mode 2 interception ingress (proxy / hooks) | **No — slot only** | Speculative until Mode 1 is validated. Do not code the adapter yet. |

"Build both" means *prepare* both (shared core + open boundary), not *implement*
both. Coding the Mode 2 adapter before Mode 1 is validated is premature
abstraction against an unvalidated second use case.

---

## Mode 1 — SDK / direct integration (beachhead, build now)

The customer's own agent code calls the gateway directly: submit the clean
prompt once, then route each tool call through the enforcer before executing.

```text
customer agent code
   ├─ submit_user_prompt(prompt)        → plan once   (pauth A1→A2→A3)
   └─ on each tool call:
        handle_tool_call(tool, args)     → enforce     (pauth B1–B4)
        → allowed → execute → record envelope (B4)
        → denied  → refuse
```

Why this is the beachhead (not just an option):

1. **It removes the hardest unsolved problem.** `DESIGN_STATUS.md` bottleneck #2
   (robustly capturing the clean prompt from an unmodified agent) **does not
   exist here** — the customer hands the clean prompt and tool calls to the SDK
   directly. No base-URL MITM, no hook removal, no TLS pinning, no TOS grey area.
2. **It removes the provider-controlled-surface strategic risk.** The
   integration point is the customer's own code, not a provider's hook surface.
   Provider incentives cannot degrade it.
3. **It ships a provable L3 product now.** Full capture + full enforcement, with
   no fragility, without waiting for interception tech to mature.

Market reality (do not romanticize):

- **Narrower segment.** Most companies use off-the-shelf agents; the set that
  builds its own agent *and* wants a third-party authorization framework is
  smaller. But it is more sophisticated, higher-value, and stickier once
  integrated. Matches the "narrow, defensible wedge" in `BUSINESS_STRATEGY.md`.
- **Heavier competition.** The "secure your own agent" space has more direct
  competitors than the unmodified-agent-firewall space (NeMo Guardrails,
  Guardrails AI, Llama Guard, agent frameworks). Differentiation must lean hard
  on **deterministic, provable task-scoping** vs ad-hoc / probabilistic checks.
- **Framework-vs-DIY tension.** A team that can build its own agent can also
  hand-roll its own checks. PAuth must be clearly better than rolling your own:
  a principled, envelope-backed, plan-once authorization framework, proven with
  honest FP/FN numbers.

---

## Mode 2 — Interception (unmodified agents; later, slot only)

For agents whose code cannot be changed (Claude Code, Codex). **Not built yet.**
Recorded here so the boundary stays designed-for, not retrofitted.

The interception sub-mode depends on the agent's auth:

| Agent auth | Interception | Notes |
|---|---|---|
| API key / API (Bedrock / Vertex / Azure) | **Inference proxy** (base-URL redirect, relay to provider) | Clean to MITM; the key is the customer's; API terms permit building on the API. The natural fit for ToB. |
| Subscription (OAuth, per-seat) | **Hooks** (`UserPromptSubmit` + `PreToolUse`) | Inference proxy is blocked: first-party-bound token, possible TLS pinning, and TOS risk. Hooks run inside the agent runtime, auth-agnostic. |

Subscription walls (why inference proxy is not viable there):

1. The OAuth token is issued for the provider's first-party use; relaying it
   through a third-party proxy is a likely TOS / "unintended use" violation.
2. TLS pinning, if present, defeats local network MITM. (Unverified for current
   Claude Code — needs real-device testing.)
3. A trust-selling product must not ship a TOS-violating MITM. Prefer explicit
   "subscription not supported; API / Team / Enterprise only".

Capture is not enforcement (applies to the inference-proxy path) — this is the
往路/復路 split made concrete:

- The inference proxy **observes** the tool calls the model emits at **復路ingress**;
  it does not by itself **block** them. Blocking requires acting on **復路egress**.
- **(A) Response rewriting** (acts on **復路egress** of the inference channel) —
  rewrite a denied `tool_use` in the model's response before it reaches the agent.
  Can gate agent-internal tools (Claude Code `Bash`, file ops) that never leave
  the agent — the only no-modification way to touch the B5 escape hatch. Fragile:
  rewriting the response mid-flight can desync the agent's state.
- **(B) Tool proxy** (acts on the **往路 of the tool-execution channel**, not the
  inference round trip) — route MCP / external tool calls through the gateway and
  deny there (`gateway/providers/mcp_suite.py`). Robust, but agent-internal tools never
  route here.
- Full L3 interception = (A) + (B) — because they cover **different legs on
  different channels**, neither alone is complete.

Prior art proving the relay is feasible (not novel): LiteLLM, Cloudflare AI
Gateway, Helicone, OpenRouter. The novel part is loading PAuth onto the relay.

---

## Open questions (not yet decided)

1. **Mode 1 SDK shape.** What is the SDK surface? Minimal: `submit_user_prompt`
   + `handle_tool_call` wrapping the existing `Gateway` class. Language bindings
   (Python first; others later?). Sync vs async. Error/deny return contract.
2. **Differentiation proof for Mode 1.** Concrete demo + honest benchmark showing
   PAuth beats hand-rolled checks and probabilistic guardrails for task-scoping.
   This is the GTM-critical artifact, not just code.
3. **Bash / internal-tool scope (Mode 2).** Reachable only via (A) response
   rewriting. Intersects the unresolved B5 / bottleneck #5 decision. Defer with
   Mode 2.
4. **Subscription support stance.** Likely "not supported; API / Team /
   Enterprise only". Confirm before any Mode 2 work.
5. **Custody.** Any interception path that sees plaintext prompts + keys must be
   **self-host only** until trust is established (`BUSINESS_OPERATIONS.md`).

## Sequencing

1. Build the shared core + keep the ingress boundary clean (mostly exists).
2. Build the **Mode 1 SDK ingress** and the differentiation demo/benchmark.
3. Land the first self-built-agent (ToB) customers on Mode 1.
4. **Only after Mode 1 is validated:** implement Mode 2 interception, starting
   with the inference-proxy + tool-proxy (API/ToB) path; treat subscription as
   out of scope unless a clear, TOS-clean mechanism exists.
5. Update `architecture.md` §1.1/§1.2 to reflect real ingress only after each
   adapter exists in code.
</content>
