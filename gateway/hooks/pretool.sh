#!/usr/bin/env bash
# Claude Code PreToolUse hook: check each tool call against the gateway plan.
#
# Stdin: JSON payload from Claude Code, containing at least
#   { "session_id": "...", "tool_name": "...", "tool_input": { ... } }
# Exit code: 0 = allow the tool call, non-zero = block.
#
# Env vars:
#   GATEWAY_URL          default http://127.0.0.1:8081
#   GATEWAY_MODE_TOOL    "strict" or "log". Default: strict (2026-09-08). With the
#                         local-tool policy below and the MCP facade, strict no
#                         longer breaks everyday Claude Code work: workspace tools
#                         pass, facade tools are enforced by the gateway, and only
#                         unobserved egress (Bash, WebFetch, foreign MCP) is blocked.
#                         Set "log" to observe without enforcing.

set -uo pipefail

GATEWAY_URL="${GATEWAY_URL:-http://127.0.0.1:8081}"
GATEWAY_MODE="${GATEWAY_MODE_TOOL:-${GATEWAY_MODE:-strict}}"

payload=$(cat)

session_id=$(printf '%s' "$payload" | /usr/bin/python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("session_id",""))')
tool_name=$(printf '%s' "$payload" | /usr/bin/python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("tool_name",""))')
tool_input_json=$(printf '%s' "$payload" | /usr/bin/python3 -c 'import sys,json; d=json.load(sys.stdin); print(json.dumps(d.get("tool_input",{})))')

if [[ -z "$session_id" || -z "$tool_name" ]]; then
  echo "[gateway-hook] missing session_id or tool_name; allowing" >&2
  exit 0
fi

# Local policy first (gateway/hooks/local_policy.py): the agent's own tools are
# allowed (workspace), denied (unobserved egress such as Bash/WebFetch/foreign
# MCP) or forwarded to the gateway. Facade tools (mcp__pauth__*) are allowed
# here because the facade itself enforces and executes them.
policy_line=$(/usr/bin/python3 "$(dirname "$0")/local_policy.py" "$tool_name" 2>/dev/null || echo "forward	policy unavailable")
policy_action="${policy_line%%	*}"
policy_reason="${policy_line#*	}"
case "$policy_action" in
  allow)
    echo "[gateway-hook] tool '$tool_name' allowed locally :: $policy_reason" >&2
    exit 0
    ;;
  deny)
    echo "[gateway-hook] tool '$tool_name' DENIED locally :: $policy_reason" >&2
    if [[ "$GATEWAY_MODE" == "log" ]]; then
      exit 0
    fi
    exit 2
    ;;
esac

body=$(/usr/bin/python3 -c '
import json, sys
tool = sys.argv[1]
ti = json.loads(sys.argv[2])
print(json.dumps({"kind": "tool_call", "tool": tool, "kwargs": ti}))
' "$tool_name" "$tool_input_json")

AUTH_HEADER=()
[[ -n "${GATEWAY_AUTH_TOKEN:-}" ]] && AUTH_HEADER=(-H "Authorization: Bearer ${GATEWAY_AUTH_TOKEN}")

response=$(curl --silent --show-error --fail-with-body \
  --max-time 30 \
  -X POST \
  -H "Content-Type: application/json" \
  "${AUTH_HEADER[@]}" \
  -d "$body" \
  "$GATEWAY_URL/sessions/$session_id/messages" 2>&1) || {
  echo "[gateway-hook] gateway HTTP error on tool '$tool_name': $response" >&2
  if [[ "$GATEWAY_MODE" == "log" ]]; then
    exit 0
  fi
  exit 2
}

permit=$(printf '%s' "$response" | /usr/bin/python3 -c 'import sys,json; d=json.load(sys.stdin); print("1" if d.get("permit") else "0")' 2>/dev/null || echo "0")
reason=$(printf '%s' "$response" | /usr/bin/python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("reason",""))' 2>/dev/null || echo "?")

if [[ "$permit" == "1" ]]; then
  echo "[gateway-hook] tool '$tool_name' permitted :: $reason" >&2
  exit 0
fi

echo "[gateway-hook] tool '$tool_name' REJECTED :: $reason" >&2
if [[ "$GATEWAY_MODE" == "log" ]]; then
  exit 0
fi
exit 2
