# Ingress design: SDK direct-connect first, interception later

This note records the decision to treat the **SDK / direct integration as the
first ingress (beachhead)** and to build the **interception ingress (proxy /
hooks) as a slot behind the same contract** later.

This note follows the discipline of `design-status.md`. It separates settled
decisions from open questions so the design does not appear more solidified than
it actually is.

Cross-references: `architecture.md` §1.1/§1.2 (ingress boundary — uses
"ingress" at the *adapter* level. See the "Terminology note" there, which points
to the directional model here. The leg model appears in architecture.md only
after interception is implemented), `design-status.md` bottleneck #2 (prompt
capture is the primary product risk).

## Core principle: the ingress mode is determined by who owns the agent

| Who owns the agent | Ingress mode | Reason |
|---|---|---|
| The customer builds it (in-house agent) | **SDK / direct integration** | The customer owns the code, so it calls the gateway (pauth core) directly. No interception needed. |
| Third-party built (unmodified Claude Code, Codex, ...) | **Interception** (inference proxy / hooks) | The code cannot be changed, so prompt and tool events must be captured from the outside. |

Both ingress modes normalize to the **same** `PromptMessage` /
`ToolCallMessage` contract (`gateway/ingress/agent_channel.py`) and flow into
the **same** deterministic core (`pauth/`). Only the ingress adapter differs.
This is exactly the use case that the loosely coupled boundary in
`architecture.md` was designed for.

Note, however: **this note uses "ingress" at two levels** — the *adapter* (SDK
vs interception, above) and the *wire-level direction* of each
capture/enforcement tap. Capture and enforcement do **not ride on the same leg**
of the round trip. Read the "Directional model" below before reading Mode 2.

## Directional model: "ingress" ≠ a single direction (request/response × ingress/egress)

The agent↔provider exchange is a **round trip**, so from the gateway's point of
view there is not one leg but four. Conflating them hides the fact that the
gateway can only *observe* on some legs and can only *enforce* on others.

```text
          request-ingress          request-egress
agent ──────────────────▶ gateway ──────────────────▶ provider
      ◀──────────────────         ◀──────────────────
          response-egress         response-ingress
```

| Leg | Wire direction | What flows | Gateway's role |
|---|---|---|---|
| **request-ingress** | agent → gateway | user prompt (request in) | **observe** the prompt → `PromptMessage`; plan-once (A1–A3) |
| **request-egress** | gateway → provider | user prompt (request out) | Relay. Optionally redact the prompt before sending |
| **response-ingress** | provider → gateway | the model's `tool_use` (response in) | **observe** the tool call → `ToolCallMessage` |
| **response-egress** | gateway → agent | response (response out) | **enforce** — rewrite/block denied `tool_use` before the agent sees it (B1–B4) |

Two consequences follow immediately:

1. **Observation lives on the ingress legs; enforcement lives on
   response-egress.** Capturing the prompt (request-ingress) and capturing the
   tool call (response-ingress) are read-only taps. To actually *stop* a tool
   call you must operate on **response-egress** — a read-write tap. This is the
   wire-level statement of "capture is not enforcement" (Mode 2, below).
2. **The two contracts correspond to the two ingress legs.** `PromptMessage` =
   request-ingress, `ToolCallMessage` = response-ingress. The core never touches
   egress directly; it only returns a decision that the **response-egress** leg
   applies.

The **tool-execution channel** (agent ↔ MCP / external tool) is a *second* round
trip with its own four legs. The tool proxy (B, below) operates not on the
inference round trip but on **its request leg** (agent → tool request).

How each mode occupies these legs:

| | request-ingress | request-egress | response-ingress | response-egress |
|---|---|---|---|---|
| **Mode 1 SDK** | `submit_user_prompt` (out-of-band call) | — (the agent calls the provider itself) | `handle_tool_call` (out-of-band call) | decision = function return value; **the agent's own code applies it** |
| **Mode 2 inference proxy** | proxy reads the request | proxy relays it | proxy reads the response | proxy rewrites/blocks it — path (A) |

**In Mode 1 the gateway is not inline.** It is a callee sitting beside the agent,
so request-egress does not exist for the gateway, and "enforcement" is merely a
boolean that the customer's code agrees to obey. **In Mode 2 the gateway is
inline**, so all four legs are real, and **the (fragile) response rewriting can
only happen on response-egress** — which is the very reason it can desync the
agent's state.

## Decisions

- **Beachhead = Mode 1 (SDK / direct), in-house-built agent / ToB segment.**
  Build this now.
- **Defer Mode 2 (interception).** Keep the ingress boundary open so it can be
  connected behind the same contract, but **do not implement the interception
  adapter yet.**
- **ToC is not a billing segment.** Provide no consumer-facing subscription-based
  payment / billing path.

Build discipline (three layers, do not collapse them):

| Layer | Build now? | Notes |
|---|---|---|
| Shared core (`pauth/`, enforcer, envelope, `AgentChannel` contract) | **Yes** | Serves both modes. It is the foundation. |
| Ingress boundary (a clean seam with a stable contract) | **Already exists** | Keep it clean so Mode 2 can be connected later. |
| Mode 1 SDK ingress | **Yes** | The beachhead. The first customers use this. |
| Mode 2 interception ingress (proxy / hooks) | **Partial — core implemented** | Hooks (`gateway/hooks/`) are operational. The proxy's enforcement core (`InterceptingProxy` in `gateway/serving/proxy.py`, S22) plus egress lockdown (`gateway/deploy/egress_lockdown.sh`) are also implemented. What remains is only the shell for TLS termination / network wiring. |

"Build both" means to *prepare* both (shared core + open boundary), not to
*implement* both. Writing the Mode 2 adapter before Mode 1 is validated is a
premature abstraction over an unvalidated second use case.

---

## Mode 1 — SDK / direct integration (beachhead, build now)

The customer's own agent code calls the gateway directly. It submits the clean
prompt once, and thereafter routes each tool call through the enforcer before
execution.

```text
customer agent code
   ├─ submit_user_prompt(prompt)        → plan once   (pauth A1→A2→A3)
   └─ on each tool call:
        handle_tool_call(tool, args)     → enforce     (pauth B1–B4)
        → allowed → execute → record envelope (B4)
        → denied  → refuse
```

Why this is the beachhead (not merely one option):

1. **It removes the hardest open problem.** `design-status.md` bottleneck #2
   (robustly capturing a clean prompt from an unmodified agent) **does not exist
   here** — the customer hands the clean prompt and tool calls to the SDK
   directly. There is no base-URL MITM, no hook removal, no TLS pinning, no TOS
   gray zone.
2. **It removes the strategic risk of a provider-controlled surface.** The
   integration point is the customer's own code, not the provider's hook
   surface. It cannot be degraded by the provider's incentives.
3. **It can ship a provable L3 product now.** Full capture + full enforcement,
   without fragility, without waiting for interception techniques to mature.

Market reality (do not gloss over it):

- **A narrower segment.** Most enterprises use off-the-shelf agents. The layer
  that builds its own agent *and* wants a third-party authorization framework is
  smaller. But it is more sophisticated, higher-value, and sticky once
  integrated. It fits the "narrow, defensible wedge" strategy.
- **Fiercer competition.** The "protect your own agent" space has more direct
  competitors than the unmodified-agent firewall space (NeMo Guardrails,
  Guardrails AI, Llama Guard, the agent frameworks). Differentiation must lean
  heavily on **deterministic, provable task-scoping** against ad-hoc /
  probabilistic checks.
- **The framework vs DIY tension.** Teams that can build their own agent can also
  hand-write their own checks. PAuth must be clearly better than a homegrown
  solution: a principled, envelope-backed, plan-once authorization framework,
  proven with honest FP/FN numbers.

---

## Mode 2 — Interception (unmodified agent; deferred, slot only)

For agents whose code cannot be changed (Claude Code, Codex). **Not built yet.**
Recorded here so the boundary stays designed-in rather than bolted-on.

The interception sub-mode depends on the agent's authentication method:

| Agent authentication | Interception | Notes |
|---|---|---|
| API key / API (Bedrock / Vertex / Azure) | **Inference proxy** (base-URL redirect, relay to provider) | The MITM is clean. The keys are the customer's. The API terms permit building on top of the API. Fits ToB naturally. |
| Subscription (OAuth, per-seat) | **Hooks** (`UserPromptSubmit` + `PreToolUse`) | The inference proxy is blocked. Tokens tied to first-party, possible TLS pinning, TOS risk. Hooks run inside the agent runtime and are independent of the authentication method. |

The subscription wall (why an inference proxy does not work there):

1. OAuth tokens are issued for the provider's first-party use. Relaying through a
   third-party proxy is likely to violate the TOS / "unintended use".
2. If TLS pinning is present, a local-network MITM is neutralized. (Not verified
   for current Claude Code — real-device testing needed.)
3. A product that sells trust must not ship a TOS-violating MITM. It is
   preferable to state explicitly that "subscription is unsupported; API / Team /
   Enterprise only."

Capture is not enforcement (applies to the inference-proxy path) — this is the
concrete form of the request/response split:

- The inference proxy **observes** the tool call the model emits on
  **response-ingress**. That alone does not **block** the tool call. To block it,
  it must operate on **response-egress**.
- **(A) Response rewriting** (operates on the inference channel's
  **response-egress**) — rewrites a denied `tool_use` in the model's response
  before it reaches the agent. It can gate tools internal to the agent that never
  leave it (Claude Code's `Bash`, file operations) — the only unmodified means
  that touches the B5 escape hatch. It is fragile. Rewriting a response mid-flight
  can desync the agent's state.
- **(B) Tool proxy** (operates not on the inference round trip but on the
  **request leg of the tool-execution channel**) — routes MCP / external tool
  calls through the gateway and denies them there
  (`gateway/providers/mcp_suite.py`). Robust, but the agent's internal tools do
  not pass through it.
- Full L3 interception = (A) + (B) — these cover **different legs on different
  channels**, so neither one alone is complete.

Prior art showing that relaying is feasible (not novel): LiteLLM, Cloudflare AI
Gateway, Helicone, OpenRouter. What is novel is putting PAuth on top of that
relay.

---

## Open questions (undecided)

1. **The shape of the Mode 1 SDK.** What should the SDK surface be? Minimal:
   `submit_user_prompt` + `handle_tool_call` wrapping the existing `Gateway`
   class. Language bindings (Python first; others later?). Sync vs async. The
   error/deny return contract.
2. **Proof of Mode 1 differentiation.** A concrete demo + honest benchmark
   showing that PAuth beats homegrown checks and probabilistic guardrails at
   task-scoping. This is not merely code but the single most important GTM
   deliverable.
3. **The scope of Bash / internal tools (Mode 2).** Reachable only via (A)
   response rewriting. Intersects the unresolved B5 / bottleneck #5 decision.
   Defer it together with Mode 2.
4. **The subscription-support policy.** Probably "unsupported; API / Team /
   Enterprise only." Settle it before starting Mode 2 work.
5. **Custody.** Every interception path that sees plaintext prompts + keys is
   **self-host only** until trust is established (`business-operations.md`).

## Sequencing

1. Build the shared core and keep the ingress boundary clean (mostly existing).
2. Build the **Mode 1 SDK ingress** and the differentiation demo/benchmark.
3. Land the first in-house-built agent (ToB) customer on Mode 1.
4. **Only after Mode 1 is validated:** implement Mode 2 interception. Start with
   the inference-proxy + tool-proxy (API/ToB) path. Treat subscription as
   out-of-scope unless a clear, TOS-clean mechanism exists.
5. Only after each adapter exists in code, update `architecture.md` §1.1/§1.2 to
   match the actual ingress.
