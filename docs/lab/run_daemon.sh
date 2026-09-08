#!/usr/bin/env bash
# Start the lab gateway daemon with the fs+git real-MCP config.
#
#   docs/lab/run_daemon.sh [config.json] [port]
#
# Reads OPENAI_API_KEY from .env if not already exported. Prints the tokens the
# agent (GATEWAY_AUTH_TOKEN) and the human operator (--operator-token) use.
set -euo pipefail
cd "$(dirname "$0")/../.."
CONFIG="${1:-docs/lab/e2e_fs_git.json}"
PORT="${2:-8091}"
RUN_DIR="${PAUTH_LAB_RUN_DIR:-/tmp/pauth-lab-run}"
mkdir -p "$RUN_DIR/plancache"
if [[ -z "${OPENAI_API_KEY:-}" && -f .env ]]; then
  export "$(grep -v '^#' .env | grep OPENAI_API_KEY | xargs)"
fi
export PAUTH_PLANNER_STRATEGY="${PAUTH_PLANNER_STRATEGY:-llm-freeform}"
export PAUTH_PLANNER_SUITE="${PAUTH_PLANNER_SUITE:-lab}"
export PAUTH_PLANNER_MODEL="${PAUTH_PLANNER_MODEL:-gpt-5.1}"
export PAUTH_PLANNER_CACHE_DIR="${PAUTH_PLANNER_CACHE_DIR:-$RUN_DIR/plancache}"
AGENT_TOKEN="${GATEWAY_AUTH_TOKEN:-labtoken}"
OPERATOR_TOKEN="${GATEWAY_OPERATOR_TOKEN:-humantoken}"
echo "agent token: $AGENT_TOKEN   operator token: $OPERATOR_TOKEN   config: $CONFIG   port: $PORT" >&2
exec .venv/bin/python gateway/serving/http_server.py --host 127.0.0.1 --port "$PORT" \
  --config "$CONFIG" \
  --session-store "$RUN_DIR/sessions.json" \
  --audit-log "$RUN_DIR/audit.jsonl" \
  --auth-token "$AGENT_TOKEN" \
  --operator-token "$OPERATOR_TOKEN"
