# Claude Code hooks for the PAuth gateway

This directory provides two Claude Code hook scripts that turn the gateway into
an actual firewall around an unmodified Claude Code session.

These hooks are part of the gateway's setup. They change neither Claude Code's
runtime nor the user's normal prompt workflow, yet they are still an explicit
integration requirement. Without the prompt hook and the pre-tool hook, the
gateway can neither reliably build a clean plan nor enforce attempted actions
before they are executed.

| Hook | Script | Purpose |
|---|---|---|
| `UserPromptSubmit` | `submit_prompt.sh` | **Before** the LLM sees the prompt, forwards the user's prompt to the gateway for plan generation. |
| `PreToolUse` | `pretool.sh` | Presents every tool call to the gateway. The gateway checks it against the valid plan and permits or rejects it. |

Both hooks communicate over `localhost` with a long-running HTTP daemon
(`gateway/serving/http_server.py`). Because session state is keyed by Claude
Code's own `session_id`, the two hooks operate on the same gateway session for
the duration of the conversation.

## 1. Start the gateway

```bash
.venv/bin/python gateway/serving/http_server.py --host 127.0.0.1 --port 8081
```

Keep this running. Restarting loses all valid sessions (`--session-store PATH`
can persist the plan-rebuild inputs. B1).

Optionally, adding `--audit-log PATH` appends permit/deny/accept/reject decisions
as JSONL (operator-facing; since it can contain values, place it where the agent
cannot read it). Check liveness with `curl http://127.0.0.1:8081/health`, and
session state with `curl http://127.0.0.1:8081/sessions/<id>` (value-free
protection level, whether a plan exists, rule count).

## 2. Add the hooks to Claude Code settings

Edit `~/.claude/settings.json` (or the project-local `.claude/settings.json`).

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "type": "command", "command": "/Users/aj/Documents/PAuthGateway/gateway/hooks/submit_prompt.sh" }
    ],
    "PreToolUse": [
      { "type": "command", "command": "/Users/aj/Documents/PAuthGateway/gateway/hooks/pretool.sh" }
    ]
  }
}
```

Adjust the absolute paths to match your own checkout.

## 3. Choose enforcement mode

The scripts honor environment variables.

| Variable | Values | Default | Effect |
|---|---|---|---|
| `GATEWAY_URL` | URL | `http://127.0.0.1:8081` | POST target. |
| `GATEWAY_MODE_PROMPT` | `strict` / `log` | `strict` | When the gateway rejects a prompt, whether to block Claude Code (`strict`) or just log and continue (`log`). |
| `GATEWAY_MODE_TOOL` | `strict` / `log` | `log` | The same setting for tool calls. Keep the default `log` while validating the integration, and switch to `strict` once the enforced tool set is fixed. |
| `GATEWAY_MODE` | `strict` / `log` | — | Fallback for when the more specific variant is unset. |
| `PAUTH_PLANNER_STRATEGY` | `deterministic` / `llm-freeform` / `interactive-structuring` / `specialized-codegen` / `formal-semantic` | `deterministic` | Chooses A1's planning strategy. |
| `PAUTH_PLANNER_SUITE` | suite name | — | Required for `llm-freeform`. Example: `shopping`. |
| `PAUTH_PLANNER_MODEL` | model id | `gpt-4.1` | Model for LLM-backed strategies. |
| `PAUTH_PLANNER_MAX_RETRIES` | integer | `3` | Retry budget for the validator feedback loop. |
| `PAUTH_PLANNER_ENABLE_JUDGE` | boolean | `true` | Enables the semantic judge for `llm-freeform`. |

Set these in the shell rc, the hook command itself, or Claude Code's `env`
block. The planner variables can be set either on the gateway daemon side or on
the prompt hook side; `submit_prompt.sh` forwards them in the prompt message when
present.

Example of the free-form planner:

```bash
PAUTH_PLANNER_STRATEGY=llm-freeform \
PAUTH_PLANNER_SUITE=shopping \
.venv/bin/python gateway/serving/http_server.py --host 127.0.0.1 --port 8081
```

## 4. Verify the round-trip

With the daemon running and the hooks installed, open Claude Code and type the
legitimate Aurora prompt.

> If the product "Aurora Noise Cancelling Headphones" is in stock and
> costs less than $150.00, add 1 to my cart and pay the cart total to
> IBAN GB33BUKB20201555555555 with subject "Order payment" on
> 2024-06-11.

The `submit_prompt.sh` hook should log `prompt accepted ::` to Claude Code's
transcript / stderr, the POST should appear in the gateway daemon's terminal, and
subsequent tool calls should appear via `pretool.sh`.

## 4b. Lock down the agent's egress (bypass prevention)

The hooks can only capture the path where the agent **cooperatively presents tool
calls**. To prevent bypassing the gateway via raw `curl` or a subprocess, run the
agent as a **dedicated non-admin user** and restrict its egress to only the
gateway's host:port. Run this once with administrator privileges.

```bash
sudo AGENT_USER=pauth-agent GATEWAY_HOST=127.0.0.1 GATEWAY_PORT=8081 gateway/deploy/egress_lockdown.sh apply
```

With this, whatever the agent runs, communication necessarily goes through the
gateway, and anything that does not is dropped at the kernel. **If you grant the
agent administrator privileges, this control is void and can be bypassed** (for
details and rationale see the "Egress Lockdown" section of
`docs/self-hosting.md`).

## Failure modes to expect

* **Gateway daemon down** → the hook shows an HTTP error. In `strict` mode this
  blocks; in `log` it permits. There is no automatic restart.
* **Prompt outside the deterministic recognizer subset** → the gateway rejects
  it, and in `strict` mode immediately blocks Claude Code. Either set
  `PAUTH_PLANNER_SUITE` and switch to `PAUTH_PLANNER_STRATEGY=llm-freeform`, or
  extend the recognizer.
* **Registered strategy not implemented** → `interactive-structuring`,
  `specialized-codegen`, and `formal-semantic` currently reject explicitly. They
  are named slots for future work, not fallbacks.
* **Tool not in the plan** → `pretool.sh` reports REJECT. In `strict` mode Claude
  Code cannot execute that tool. In `log` mode it continues, but the rejection is
  recorded in the log. Useful for measuring actual Claude Code behavior before
  committing to enforcement.

For the whole-system design see `docs/architecture.md`.
