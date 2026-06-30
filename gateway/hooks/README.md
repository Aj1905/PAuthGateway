# Claude Code hooks for the PAuth gateway

This directory ships two Claude Code hook scripts that turn the gateway
into a real firewall around an unmodified Claude Code session:

These hooks are part of gateway setup. They do not modify Claude Code's
runtime or the user's normal prompt workflow, but they are still an explicit
integration requirement: without a prompt hook and a pre-tool hook, the gateway
cannot reliably build a clean plan or enforce attempted actions before they
run.

| Hook | Script | Purpose |
|---|---|---|
| `UserPromptSubmit` | `submit_prompt.sh` | Forward the user's prompt to the gateway for plan generation, **before** the LLM sees it. |
| `PreToolUse` | `pretool.sh` | Offer every tool call to the gateway, which checks it against the active plan and permits or rejects. |

Both hooks talk to a long-running HTTP daemon (`gateway/serving/http_server.py`)
over `localhost`. Session state is keyed by Claude Code's own
`session_id`, so the two hooks operate on the same gateway session for
the duration of a conversation.

## 1. Start the gateway

```bash
.venv/bin/python gateway/serving/http_server.py --host 127.0.0.1 --port 8081
```

Leave this running. Restarting drops every active session.

## 2. Add the hooks to Claude Code settings

Edit `~/.claude/settings.json` (or the project-local `.claude/settings.json`):

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

Adjust the absolute path for your checkout.

## 3. Choose enforcement mode

The scripts honor environment variables:

| Variable | Values | Default | Effect |
|---|---|---|---|
| `GATEWAY_URL` | URL | `http://127.0.0.1:8081` | Where to POST. |
| `GATEWAY_MODE_PROMPT` | `strict` / `log` | `strict` | If the gateway rejects the prompt, block Claude Code (`strict`) or just log and proceed (`log`). |
| `GATEWAY_MODE_TOOL` | `strict` / `log` | `log` | Same for tool calls. Default is `log` while integration is being validated; flip to `strict` once the enforced tool set is finalised. |
| `GATEWAY_MODE` | `strict` / `log` | — | Fallback when the more-specific variant is unset. |
| `PAUTH_PLANNER_STRATEGY` | `deterministic` / `llm-freeform` / `interactive-structuring` / `specialized-codegen` / `formal-semantic` | `deterministic` | Selects the A1 planning strategy. |
| `PAUTH_PLANNER_SUITE` | suite name | — | Required by `llm-freeform`, e.g. `shopping`. |
| `PAUTH_PLANNER_MODEL` | model id | `gpt-4.1` | Model for LLM-backed strategies. |
| `PAUTH_PLANNER_MAX_RETRIES` | integer | `3` | Retry budget for validator feedback loops. |
| `PAUTH_PLANNER_ENABLE_JUDGE` | boolean | `true` | Enables the semantic judge for `llm-freeform`. |

Set them in your shell rc, in the hook command itself, or via Claude
Code's `env` block. Planner variables can be set either on the gateway
daemon or on the prompt hook; `submit_prompt.sh` forwards them in the
prompt message when present.

Example free-form planner:

```bash
PAUTH_PLANNER_STRATEGY=llm-freeform \
PAUTH_PLANNER_SUITE=shopping \
.venv/bin/python gateway/serving/http_server.py --host 127.0.0.1 --port 8081
```

## 4. Verify the round-trip

With the daemon running and the hooks installed, open Claude Code and
type the canonical Aurora prompt:

> If the product "Aurora Noise Cancelling Headphones" is in stock and
> costs less than $150.00, add 1 to my cart and pay the cart total to
> IBAN GB33BUKB20201555555555 with subject "Order payment" on
> 2024-06-11.

The `submit_prompt.sh` hook should log `prompt accepted ::` to the
Claude Code transcript / stderr, the gateway daemon's terminal should
show the POST, and subsequent tool calls show up via `pretool.sh`.

## Failure modes to expect

* **Gateway daemon down** → hook prints an HTTP error. With `strict`
  mode this blocks; with `log` it allows. There is no automatic restart.
* **Prompt outside the deterministic recognizer subset** → gateway
  rejects, `strict` mode blocks Claude Code immediately. Switch to
  `PAUTH_PLANNER_STRATEGY=llm-freeform` with `PAUTH_PLANNER_SUITE`
  set, or extend the recognizer.
* **Registered strategy not implemented** → `interactive-structuring`,
  `specialized-codegen`, and `formal-semantic` currently reject
  explicitly. They are named slots for future work, not fallbacks.
* **Tool not in the plan** → `pretool.sh` reports REJECT. In `strict`
  mode Claude Code can't run that tool. In `log` mode it proceeds but
  the rejection is in the log -- useful for measuring real Claude Code
  behaviour before committing to enforcement.

See `architecture.md` for the system-wide design and `grill.md` for the
decision history (Q10 capture mechanism, Q13 trust shift, etc.).
