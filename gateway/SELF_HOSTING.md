# Self-hosted gateway direction

This document records the product target and the engineering boundary for
turning the current PAuth gateway into a self-hosted app that existing agent
users can connect without changing their day-to-day agent workflow.

## Target

The user should not edit agent prompts, agent code, or tool definitions. The
target is: after initial setup, the user's normal agent workflow should feel
unchanged while the gateway observes and enforces every outward action.

Initial setup is allowed to install/enable a gateway integration. In practice
that means both:

1. a lifecycle hook/plugin that forwards the clean user prompt and attempted
   tool calls to the gateway;
2. a network/tool route that prevents the agent from bypassing the gateway for
   outward actions.

That target is feasible only when the gateway can observe two things:

1. the user's task prompt before tool-result injection can affect planning;
2. every outward tool call with concrete tool name and arguments.

If a target agent encrypts, hides, or internally executes either side without a
hook/proxy-observable protocol boundary, transparent network config alone
cannot provide PAuth enforcement. It can at best provide coarse allow/deny by
destination.

## Setup Boundary

The best near-term product contract is not "zero setup". It is:

- **No agent code changes**: the agent binary/runtime remains unmodified.
- **No prompt workflow changes**: the user keeps typing tasks into the agent.
- **Gateway setup required once**: install/enable hook/plugin, configure
  gateway URL, configure strict/log mode, and route registered tools/API calls
  through the gateway.
- **Observable health**: the gateway must surface whether hooks and tool routes
  are active. Silent failure is worse than no protection.

Alternatives that promise less setup are weaker:

| Alternative | Why it is not enough |
|---|---|
| Pure network/TLS proxy | Usually cannot recover clean prompt, semantic tool name, or structured arguments. |
| SaaS-side only enforcement | Requires every SaaS to adopt PAuth or expose compatible policy hooks. |
| Agent-vendor native integration | Best UX, but depends on vendor adoption and is not self-host controlled. |
| Browser/OS observation only | Fragile and hard to make deterministic or portable. |

So the practical architecture is a **gateway app plus agent integration**:
network firewalling alone is not the security boundary; lifecycle hooks provide
the semantic events PAuth needs.

## Prompt Capture Boundary

The gateway cannot realistically obtain prompts from every agent by one
mechanism. The target is not "same capture mechanism"; it is "same normalized
event after capture".

Every integration should translate its native prompt signal into:

```json
{
  "kind": "prompt",
  "session_id": "agent-session-id",
  "prompt": "clean user task text",
  "source": "claude-code-hook | mcp-session | browser-extension | desktop-plugin | manual",
  "captured_before_model": true
}
```

Current code uses `PromptMessage` inside `gateway/ingress/agent_channel.py` as the
normalized prompt event. Future prompt-capture adapters should feed that same
boundary.

Prompt capture options, from strongest to weakest:

| Capture route | Strength | Problem |
|---|---|---|
| Agent lifecycle hook/plugin | Best near-term route when available. Captures prompt before tool execution. | Per-agent integration work. |
| Gateway-owned prompt entrypoint | Strongest integrity: user enters task through gateway first. | Changes user workflow. Use only when acceptable. |
| MCP/session metadata | Potentially good if the agent sends task metadata to tool servers. | Not universally available or standardized. |
| Browser/desktop extension | Can support agents without hooks. | Fragile, app-specific, harder to prove ordering. |
| Manual confirmation fallback | Useful for safety-critical actions. | Changes workflow and adds friction. |

The gateway should track a protection level per session:

| Level | Observed by gateway | PAuth claim |
|---|---|---|
| L0 | Network destination only | No PAuth guarantee; coarse firewall only. |
| L1 | Tool call only | Can deny unknown/off-policy tools, but cannot derive a user-intent plan. |
| L2 | Clean prompt + tool call | PAuth plan enforcement is meaningful. |
| L3 | Clean prompt + tool call + gateway-executed tools | Strongest current model; envelope provenance is reliable. |

Production messaging must not call L0/L1 "complete protection". The PAuth-style
claim starts at L2, and the design target is L3.

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

The stable contract is the normalized message boundary:

- `PromptMessage`: task prompt and planner options.
- `ToolCallMessage`: concrete tool name plus ordered or named arguments.

Everything before that boundary is adapter/proxy work. Everything after the
planner boundary is PAuth's deterministic core.

## Planner Strategy

The A1 logic is intentionally volatile. The gateway must treat it as a
replaceable strategy, not as part of the HTTP/proxy surface.

The strategy catalogue lives in `gateway/PLANNING_STRATEGIES.md`. The three
near-term slots are:

- interactive structuring through a "Grill me" style clarification loop;
- specialized imperative-code generation model plus validator retries;
- formal natural-language analysis for narrow controlled-language domains.

The runtime strategy switch is named `PAUTH_PLANNER_STRATEGY` for the current
HTTP/hook deployment. The canonical values are `deterministic`,
`llm-freeform`, `interactive-structuring`, `specialized-codegen`, and
`formal-semantic`. A future packaged app can move the same names into a config
file or UI setting without changing the planner boundary.

Current strategies:

- `DeterministicRecognizerPlanner`: strict regex subset; good for tests and
  high-confidence demos.
- `LLMFreeformPlanner`: general prompt-to-code generation with grammar repair
  and optional intent judge.

Future strategies should implement the same planner shape:

- fine-tuned prompt-to-code model;
- remote planner service;
- human-reviewed plan approval;
- suite-specific planner;
- hybrid retrieval plus LLM planner.

The invariant is that every strategy emits restricted imperative `run` code,
then `pauth.prepare()` validates grammar, derives slices, and compiles rules.
No planner is allowed to bypass that deterministic validation.

## Self-hosted Foundation

Minimum viable self-hosted app:

1. **Configurable upstream registry**
   - Register MCP/HTTP/SaaS tool sources.
   - Preserve tool schemas, parameter order, and return schemas.
   - Reject tool-name collisions unless explicitly namespaced.
   - Reflect OpenAPI specs into `SuiteSpec` for HTTP APIs.
   - Detect upstream API spec changes and emit a user-notifiable report before
     accepting the new tool surface.

2. **Ingress adapters**
   - Start with MCP/HTTP because their tool boundaries are visible.
   - Keep Claude Code hooks as a compatibility adapter, not the product core.
   - Add protocol-specific adapters only when they can expose prompt and tool
     calls without trusting the agent to rewrite them.

3. **Planner plugin boundary**
   - Select planner per deployment/session.
   - Store generated code, validation failures, and retry history for audit.
   - Default production posture should be reject-on-uncertain, not fabricate.

4. **Session and audit store**
   - Persist prompt, selected planner, generated code, compiled rule summary,
     decisions, denials, and upstream call results.
   - Keep envelope signing keys local to the deployment.

5. **Operations surface**
   - Single config file for tool sources and planner mode.
   - Health checks for upstream tools and planner credentials.
   - Strict/log mode per source while onboarding.
   - Scheduled API-spec monitor for configured OpenAPI sources.

## API Spec Reflection And Change Notification

OpenAPI-backed suites can be registered in gateway config:

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

At gateway startup, `gateway/providers/openapi_suite.py` reflects the current OpenAPI
document into a `SuiteSpec`: operations become tools, parameters and JSON body
fields become tool operands, and response schemas become A1 tool docs.

For notification/update workflows, run:

```bash
.venv/bin/python -m gateway.api_spec_monitor \
  --config gateway.json \
  --state .gateway/api-spec-state.json \
  --update
```

The monitor emits JSON describing changed specs, added/removed tools, and
changed parameter lists. A self-hosted deployment can wire that JSON to email,
Slack, app UI notifications, or a restart/reload workflow.

Current limitation: a running `gateway/serving/http_server.py` does not hot-reload a
changed OpenAPI spec. The next layer should add an authenticated reload endpoint
or a supervisor-managed restart after the user accepts the changed tool
surface.

## Non-goals For The First Cut

- Generic TLS MITM of arbitrary agents. That is operationally brittle and does
  not recover semantic tool names or arguments by itself.
- Claiming prompt-to-code correctness. PAuth enforces a generated plan; it does
  not prove the plan faithfully captures the user's intent.
- Full SaaS multi-tenant hosting. The near-term target is self-hosted,
  single-user or single-team deployment.

## Immediate Engineering Order

1. Keep `pauth/` stable and framework-neutral.
2. Move all A1 variants behind `gateway.planner`.
3. Treat `AgentChannel`'s JSON messages as the internal normalized protocol.
4. Build ingress adapters that translate real agent traffic into that protocol.
5. Add persistence/audit after the normalized protocol is stable.
