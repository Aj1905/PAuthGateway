# Self-hosted gateway direction

This document records the product goals and engineering boundaries for turning
the current PAuth gateway into a self-hosted app that existing agent users can
connect to without changing their day-to-day agent workflow.

## Target

The user should not have to edit the agent's prompt, the agent's code, or the
tool definitions. The goal is this: after initial setup, the user's normal agent
workflow feels unchanged, while behind the scenes the gateway observes and
enforces every outbound action.

Initial setup may involve installing/enabling the gateway integration. In
practice this means both of the following.

1. A lifecycle hook/plugin that forwards the clean user prompt and attempted tool
   calls to the gateway.
2. A network/tool route that prevents the agent from bypassing the gateway on
   outbound actions.

This goal holds only when the gateway can observe the following two things.

1. The user's task prompt, before tool-result injection can influence the plan.
2. Every outbound tool call, with concrete tool names and arguments.

If the target agent encrypts, hides, or internally executes either of these
without passing through a protocol boundary observable via hook/proxy, then a
transparent network configuration alone cannot provide PAuth enforcement. At
best it can provide coarse per-destination allow/deny.

## Setup Boundary

The best product contract in the near term is not "zero setup." It is the
following.

- **No agent code changes**: the agent's binary/runtime stays unmodified.
- **No prompt workflow changes**: the user keeps typing tasks into the agent.
- **Gateway setup required once**: install/enable the hook/plugin, configure the
  gateway URL, set strict/log mode, and route registered tool/API calls through
  the gateway.
- **Observable health**: the gateway must make visible whether the hook and tool
  route are active. Silent failure is worse than no protection.

Every alternative that promises lighter setup is weaker.

| Alternative | Why it is not enough |
|---|---|
| Pure network/TLS proxy | Typically cannot recover the clean prompt, the semantic tool name, or the structured arguments. |
| SaaS-side only enforcement | Requires every SaaS to adopt PAuth or expose a compatible policy hook. |
| Agent-vendor native integration | Best UX, but depends on vendor adoption and cannot be controlled from the self-hosted side. |
| Browser/OS observation only | Fragile, and hard to make deterministic and portable. |

Therefore the practical architecture is **gateway app plus agent integration**.
The network firewall is not the only security boundary; the lifecycle hook
supplies the semantic events PAuth needs.

## Protection Precondition: No Raw Side Channels (Stage 1)

The Stage 1 protection claim holds only under the following preconditions
(decision records: S6, B5).

- The agent **does not** have raw Bash, direct network I/O, or unobserved
  credential paths. Every outbound action is a tool call through the gateway.
- A deployment that does not satisfy this precondition (e.g. Claude Code with
  Bash enabled) must not be marketed as L3 protection. Report the effective
  protection level honestly as L1/L2.
- Side-channel gate mechanisms (allowlist / sandbox / FS virtualization /
  response rewriting) are the agenda of Stage 6 (Mode 2) and are not included in
  Stage 1.

## Egress Lockdown: turning the Stage 1 precondition from "assumption" into "enforcement"

Enforce the above precondition "the agent has no raw Bash and no direct network
I/O" **through the OS network path at setup time**. Without this, the precondition
is a mere assumption, and paths that bypass the PreToolUse hook (a subprocess
spawned by an allowed tool, an MCP server that drops to a shell, a tool that
execs `curl` on injection) pass through unchecked (case B).

### What to do

Run the agent app (Claude Code / Codex etc.) as a **dedicated non-admin user**,
and restrict that user's outbound communication to **only the gateway's
host:port**. As part of setup, run the following once with admin privileges.

```
sudo AGENT_USER=pauth-agent GATEWAY_HOST=127.0.0.1 GATEWAY_PORT=8081 gateway/deploy/egress_lockdown.sh apply
```

`gateway/deploy/egress_lockdown.sh` applies this as an `uid-owner` rule in
nftables/iptables on Linux, and as a pf `user` rule on macOS. Because child
processes inherit the UID, whatever command the agent issues, the entire tree
falls under the same restriction.

### Why you can block it even without reading the traffic content

Once the destination is pinned to the gateway, a bypass attempt has only two
outcomes, and both fail.

1. **Bound for an external host** (e.g. `curl bank.example`) → this rule
   **drops it in the kernel**. It never leaves. The gateway never even sees it.
2. **Bound for the gateway** (the only reachable destination) → the gateway
   receives it, and because it is **default-deny**, it rejects anything that is
   not a legitimately authorized tool call.

Therefore the gateway **does not need to decode the bytes** of an opaque `curl`.
Opaque traffic either "has nowhere to go (drop)" or "arrives at a default-deny
gateway (reject)," so it always fail-closes. This is a "neutralize rather than
decode" design, and it is the foundation for raising effective protection from an
honest L0/L1 to L2/L3.

### Granting admin privileges bypasses this (must be stated)

**This control holds only on top of "the agent cannot remove the rule."** If you
run the agent as root / admin / a sudo-capable user, the agent (or code made to
run by injection) can flush the firewall rule, reach an external server directly,
and **bypass the gateway**. The script refuses to apply when the target is root,
and prints this warning after applying. **Do not grant the agent admin
privileges.** The moment you do, this control is void, and the effective
protection level must be honestly reported as L1/L2.

### What it does not cover (handle separately)

- **Non-network side effects** (local file tampering, planting secrets) cannot be
  prevented by egress restriction. Constrain them separately on the filesystem
  side.
- **The gateway's own egress**: the gateway needs to reach the real SaaS, so run
  it as a **separate user** from the agent so it is not caught by this rule.
- DNS / IPv6 / QUIC (UDP): the script drops the agent's external DNS, IPv6, and
  UDP together (the agent needs no external DNS because the gateway makes the real
  calls). Closed-source code that uses DoH pinned to its own resolver needs
  separate blocking of the DoH destinations.

## Prompt Capture Boundary

It is not realistic for the gateway to capture the prompt from every agent with a
single mechanism. The goal is not "the same capture mechanism" but "the same
normalized event after capture."

Every integration should translate the native prompt signal into the following
form.

```json
{
  "kind": "prompt",
  "session_id": "agent-session-id",
  "prompt": "clean user task text",
  "source": "claude-code-hook | mcp-session | browser-extension | desktop-plugin | manual",
  "captured_before_model": true
}
```

The current code uses `PromptMessage` in `gateway/ingress/agent_channel.py` as the
normalized prompt event. Future prompt-capture adapters should also feed the same
boundary.

The prompt-capture options, ordered from strongest to weakest.

| Capture route | Strength | Problem |
|---|---|---|
| Agent lifecycle hook/plugin | The best near-term route when available. Captures the prompt before tool execution. | Requires per-agent integration work. |
| Gateway-owned prompt entrypoint | Highest completeness. The user enters the task through the gateway first. | Changes the user workflow. Use only when acceptable. |
| MCP/session metadata | Promising when the agent sends task metadata to the tool server. | Neither universally available nor standardized. |
| Browser/desktop extension | Can help agents that have no hook. | Fragile, app-specific, and hard to prove ordering for. |
| Manual confirmation fallback | Useful for safety-critical actions. | Changes the workflow and adds friction. |

The gateway should track the protection level (L0–L3) per session. **The canonical
definition of L0–L3 is `ProtectionLevel` in the code `gateway/runtime/protection.py`**
(the human-facing table is in `design-status.md`, "Current protection model").
Here only the essentials:

- **Do not call anything below L2 "full protection."** The PAuth-style claim
  starts at L2 (clean prompt + tool call), and the design goal is L3 (+ gateway
  that executes the tools).
- A session that lacks clean-prompt capture or route control is not marketed as
  full PAuth protection.

## Architecture Shape

```text
agent runtime
  |
  | hook/plugin + network/tool route
  v
gateway ingress
  |
  | normalized PromptMessage / ToolCallMessage
  v
planner boundary (A1)
  |
  | restricted imperative code
  v
pauth.prepare() -> rules
  |
  | per-call enforcement
  v
upstream tool/SaaS/MCP server
```

The stable contract is the normalized message boundary.

- `PromptMessage`: the task prompt and planner options.
- `ToolCallMessage`: the concrete tool name and its ordered or named arguments.

Everything before this boundary is adapter/proxy work. Everything after the
planner boundary is the deterministic core of PAuth.

## Planner Strategy

The A1 logic is intentionally volatile. The gateway must treat it as a replaceable
strategy rather than part of the HTTP/proxy surface.

The strategy catalog is in `planning-strategies.md`. The three near-term frames
are the following.

- Interactive structuring via a "Grill me"-style clarification loop.
- A dedicated imperative-code generation model with a validator retry.
- Formal natural-language parsing for a narrow control-language domain.

Runtime strategy switching is named `PAUTH_PLANNER_STRATEGY` in the current
HTTP/hook deployment. The canonical values are `deterministic`, `llm-freeform`,
`interactive-structuring`, `specialized-codegen`, and `formal-semantic`. A future
packaged app can move the same names into a config file or UI setting without
changing the planner boundary.

Current strategies:

- `DeterministicRecognizerPlanner`: a strict regex subset. Suited to tests and
  high-confidence demos.
- `LLMFreeformPlanner`: general-purpose prompt-to-code generation with grammar
  repair and an optional intent judge.

Future strategies should implement the same planner shape.

- A fine-tuned prompt-to-code model.
- A remote planner service.
- Human-reviewed plan approval.
- A suite-specific planner.
- A hybrid planner combining search and an LLM.

The invariant is that every strategy emits restricted imperative `run` code, after
which `pauth.prepare()` validates the grammar, derives the slices, and compiles
the rules. No planner is allowed to bypass that deterministic validation.

## Self-hosted Foundation

The minimal self-hosted app:

1. **Configurable upstream registry**
   - Register MCP/HTTP/SaaS tool sources.
   - Retain tool schemas, parameter order, and return schemas.
   - Reject tool-name collisions unless explicitly namespaced.
   - For HTTP APIs, reflect the OpenAPI spec into a `SuiteSpec`.
   - Detect changes in the upstream API spec and issue a report that can notify
     the user before accepting the new tool surface.

2. **Ingress adapters**
   - Start with MCP/HTTP, where the tool boundary is visible.
   - Keep Claude Code hooks as a compatibility adapter, not the product core.
   - Add protocol-specific adapters only when they can expose the prompt and tool
     calls without trusting the agent's rewriting.

3. **Planner plugin boundary**
   - Choose the planner per deployment/session.
   - Store generated code, validation failures, and retry history for audit.
   - The default production posture should be reject-on-uncertain rather than
     fabrication.

4. **Session and audit store**
   - Persist the prompt, the selected planner, the generated code, the compiled
     rule summary, decisions, rejections, and upstream call results.
   - Keep the envelope signing key local to the deployment.

5. **Operations surface**
   - A single config file for tool sources and planner mode.
   - Health checks for upstream tools and planner credentials.
   - Per-source strict/log mode during onboarding.
   - A periodic API-spec monitor for configured OpenAPI sources.

## API Spec Reflection And Change Notification

An OpenAPI-backed suite can be registered in the gateway config.

```json
{
  "merged_suite_name": "user_default",
  "suites": [
    {
      "name": "billing",
      "kind": "openapi",
      "spec_path": "billing.openapi.json",
      "base_url": "https://api.example.com"
    }
  ]
}
```

At gateway startup, `gateway/providers/openapi_suite.py` reflects the current
OpenAPI document into a `SuiteSpec`. Operations become tools, parameters and JSON
body fields become the tool's operands, and the response schema becomes the A1
tool documentation.

For the notification/update workflow, run the following.

```bash
.venv/bin/python -m gateway.api_spec_monitor \
  --config gateway.json \
  --state .gateway/api-spec-state.json \
  --update
```

This monitor emits JSON describing the changed spec, the added/removed tools, and
the changed parameter lists. A self-hosted deployment can wire that JSON into
email, Slack, an app UI notification, or a restart/reload workflow.

Current limitation: the running `gateway/serving/http_server.py` does not hot-reload
a changed OpenAPI spec. The next layer should add an authenticated reload
endpoint, or a supervisor-managed restart after the user accepts the changed tool
surface.

## Non-goals For The First Cut

- Generic TLS MITM against arbitrary agents. Operationally fragile, and on its own
  it cannot recover semantic tool names or arguments.
- Claims about the correctness of prompt-to-code. PAuth enforces the generated
  plan; it does not prove that the plan faithfully captures the user's intent.
- Full SaaS multi-tenant hosting. The near-term goal is a self-hosted,
  single-user or single-team deployment.

## Immediate Engineering Order

1. Keep `pauth/` stable and framework-neutral.
2. Move all A1 variants behind `gateway.planner`.
3. Treat the `AgentChannel` JSON messages as the internal normalized protocol.
4. Build ingress adapters that translate real agent traffic into that protocol.
5. Add persistence/audit after the normalized protocol has stabilized.
