#!/usr/bin/env bash
# Claude Code PreToolUse hook: check each tool call against the gateway plan.
#
# Stdin: JSON payload from Claude Code, containing at least
#   { "session_id": "...", "tool_name": "...", "tool_input": { ... } }
# Exit code: 0 = allow the tool call, non-zero = block.
#
# Env vars:
#   GATEWAY_URL          default http://127.0.0.1:8081
#   GATEWAY_MODE_TOOL    "strict" or "log". Default: log (don't break Claude Code
#                         while integration is being validated; flip to strict when
#                         the suite of enforced tools is established).

set -uo pipefail

GATEWAY_URL="${GATEWAY_URL:-http://127.0.0.1:8081}"
GATEWAY_MODE="${GATEWAY_MODE_TOOL:-${GATEWAY_MODE:-log}}"

payload=$(cat)

session_id=$(printf '%s' "$payload" | /usr/bin/python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("session_id",""))')
tool_name=$(printf '%s' "$payload" | /usr/bin/python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("tool_name",""))')
tool_input_json=$(printf '%s' "$payload" | /usr/bin/python3 -c 'import sys,json; d=json.load(sys.stdin); print(json.dumps(d.get("tool_input",{})))')

if [[ -z "$session_id" || -z "$tool_name" ]]; then
  echo "[gateway-hook] missing session_id or tool_name; allowing" >&2
  exit 0
fi

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
