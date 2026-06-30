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
#   PAUTH_PLANNER_STRATEGY   deterministic / llm-freeform / interactive-structuring /
#                            specialized-codegen / formal-semantic
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
print(json.dumps(body))
' "$prompt")

response=$(curl --silent --show-error --fail-with-body \
  --max-time 30 \
  -X POST \
  -H "Content-Type: application/json" \
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

if [[ "$accepted" == "1" ]]; then
  echo "[gateway-hook] prompt accepted :: $reason" >&2
  exit 0
fi

echo "[gateway-hook] prompt REJECTED by gateway :: $reason" >&2
if [[ "$GATEWAY_MODE" == "log" ]]; then
  exit 0
fi
exit 2
