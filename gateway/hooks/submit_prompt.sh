#!/usr/bin/env bash
# Claude Code UserPromptSubmit hook: forward the user's prompt to the gateway.
#
# Stdin: JSON payload from Claude Code, containing at least
#   { "session_id": "...", "prompt": "..." }
# Exit code: 0 = allow Claude Code to proceed, non-zero = block.
#
# Env vars (optional):
#   GATEWAY_URL              base URL of gateway/serving/http_server.py (default: http://127.0.0.1:8081)
#   GATEWAY_MODE             "strict" (block on gateway reject) or "log" (log only).
#   PAUTH_PLANNER_STRATEGY   deterministic / llm-freeform / auto / sufficiency-tightness /
#                            interactive-structuring / specialized-codegen / formal-semantic
#                            (default: auto, resolved by AgentChannel)
#   PAUTH_PLANNER_SUITE      suite name required by llm-freeform, e.g. shopping
#   PAUTH_PLANNER_MODEL      model id for LLM-backed strategies
#   PAUTH_PLANNER_MAX_RETRIES retry budget for validator feedback loops

set -uo pipefail

GATEWAY_URL="${GATEWAY_URL:-http://127.0.0.1:8081}"
GATEWAY_MODE="${GATEWAY_MODE_PROMPT:-${GATEWAY_MODE:-strict}}"

payload=$(cat)

session_id=$(printf '%s' "$payload" | /usr/bin/python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("session_id",""))')
prompt=$(printf '%s' "$payload" | /usr/bin/python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("prompt",""))')

if [[ -z "$session_id" || -z "$prompt" ]]; then
  echo "[gateway-hook] missing session_id or prompt; allowing by default" >&2
  exit 0
fi

body=$(/usr/bin/python3 -c '
import json, os, sys
p = sys.argv[1]
body = {"kind": "prompt", "prompt": p}
mapping = {
    "PAUTH_PLANNER_STRATEGY": "strategy",
    "PAUTH_PLANNER_SUITE": "suite_name",
    "PAUTH_PLANNER_MODEL": "model",
    "PAUTH_PLANNER_CACHE_DIR": "cache_dir",
    "PAUTH_PLANNER_JUDGE_MODEL": "judge_model",
}
for env_name, field in mapping.items():
    value = os.environ.get(env_name)
    if value:
        body[field] = value
if os.environ.get("PAUTH_PLANNER_MAX_RETRIES"):
    body["max_retries"] = int(os.environ["PAUTH_PLANNER_MAX_RETRIES"])
if os.environ.get("PAUTH_PLANNER_ENABLE_JUDGE"):
    body["enable_judge"] = os.environ["PAUTH_PLANNER_ENABLE_JUDGE"].lower() in {"1", "true", "yes", "on"}
# Bind the session to this agent process so the MCP facade spawned by the same
# process (gateway/serving/mcp_facade.py) can find the plan without a session
# id in the MCP protocol. Claude Code exports CLAUDE_PID to hooks and servers.
if os.environ.get("CLAUDE_PID"):
    body["binding"] = "pid:" + os.environ["CLAUDE_PID"]
print(json.dumps(body))
' "$prompt")

AUTH_HEADER=()
[[ -n "${GATEWAY_AUTH_TOKEN:-}" ]] && AUTH_HEADER=(-H "Authorization: Bearer ${GATEWAY_AUTH_TOKEN}")

# Deployment health at the start of every task (gateway/operator/health.py,
# codex T6): hooks registered, egress lockdown present, daemon reachable.
# Reported, not enforced: an unverifiable check ("unknown") is not a failure of
# the plan, but the user must see that a protection is not confirmed.
if [[ "${GATEWAY_HEALTH_CHECK:-1}" != "0" ]]; then
  health=$(cd "$(dirname "$0")/../.." && PYTHONPATH=. /usr/bin/python3 -m gateway.operator.health --url "$GATEWAY_URL" 2>/dev/null) || true
  if [[ -z "$health" ]]; then
    # The probe itself did not run (python missing, module absent): say so
    # rather than silently passing -- an unverified protection is not a
    # confirmed one (codex, 2026-09-08).
    echo "[gateway-hook] deployment health probe unavailable :: health_probe_unavailable" >&2
  else
    healthy=$(printf '%s' "$health" | /usr/bin/python3 -c 'import sys,json; d=json.load(sys.stdin); print("1" if d.get("healthy") else "0")' 2>/dev/null || echo "unparseable")
    if [[ "$healthy" == "unparseable" ]]; then
      echo "[gateway-hook] deployment health probe returned unreadable output :: health_probe_unreadable" >&2
    elif [[ "$healthy" != "1" ]]; then
      echo "[gateway-hook] deployment health NOT confirmed :: $health" >&2
    fi
  fi
fi

response=$(curl --silent --show-error --fail-with-body \
  --max-time 30 \
  -X POST \
  -H "Content-Type: application/json" \
  "${AUTH_HEADER[@]}" \
  -d "$body" \
  "$GATEWAY_URL/sessions/$session_id/messages" 2>&1) || {
  echo "[gateway-hook] gateway HTTP error: $response" >&2
  if [[ "$GATEWAY_MODE" == "log" ]]; then
    exit 0
  fi
  exit 2
}

accepted=$(printf '%s' "$response" | /usr/bin/python3 -c 'import sys,json; d=json.load(sys.stdin); print("1" if d.get("accepted") else "0")' 2>/dev/null || echo "0")
reason=$(printf '%s' "$response" | /usr/bin/python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("reason",""))' 2>/dev/null || echo "?")
empty_plan=$(printf '%s' "$response" | /usr/bin/python3 -c 'import sys,json; d=json.load(sys.stdin); print("1" if d.get("empty_plan") else "0")' 2>/dev/null || echo "0")

if [[ "$accepted" == "1" ]]; then
  echo "[gateway-hook] prompt accepted :: $reason" >&2
  exit 0
fi

# A rejected plan (empty, or one the Planner could not express) leaves every
# gateway tool default-denied for this session. The agent's own local tools
# are outside the gateway either way, so letting the conversation continue is
# safe and is what a Claude Code user expects; blocking it protects nothing.
# GATEWAY_PLAN_REJECT=block restores the paper-faithful "no plan, no agent".
if [[ "${GATEWAY_PLAN_REJECT:-continue}" == "continue" ]]; then
  if [[ "$empty_plan" == "1" ]]; then
    echo "[gateway-hook] no gateway tool is needed for this prompt (gateway tools stay denied) :: $reason" >&2
  else
    echo "[gateway-hook] plan rejected; gateway tools stay denied for this session, continuing :: $reason" >&2
  fi
  exit 0
fi

echo "[gateway-hook] prompt REJECTED by gateway :: $reason" >&2
if [[ "$GATEWAY_MODE" == "log" ]]; then
  exit 0
fi
exit 2
