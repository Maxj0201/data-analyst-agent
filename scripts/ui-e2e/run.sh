#!/usr/bin/env bash
# Starts the dry-run API (real API + AgentLoop + sandbox + ApprovalBroker, scripted LLM) and the
# Streamlit UI, then drives the UI in headless Chromium (ui_e2e.js). Used by CI (ui-e2e job) and
# locally:  scripts/ui-e2e/run.sh [out_dir]
#
# --docker: the same checks against the Docker images instead. Builds both targets, starts the
# compose stack (project `analyst-agent-e2e`, ports 8000/8501) with docker-compose.e2e.yml on
# top, which swaps the API command for the dry-run harness and turns every flag on, then
# removes the stack and its volumes. Base images can be overridden with PYTHON_IMAGE/UV_IMAGE.
#   scripts/ui-e2e/run.sh --docker [out_dir]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HERE="$ROOT/scripts/ui-e2e"
DOCKER=false
if [[ "${1:-}" == "--docker" ]]; then
  DOCKER=true
  shift
fi
OUT="${1:-${TMPDIR:-/tmp}/ui-e2e}"
API_PORT="${API_PORT:-8100}"
UI_PORT="${UI_PORT:-8601}"
mkdir -p "$OUT/data" "$OUT/art"
cd "$ROOT"

# Every AGENT_*_ENABLED flag is on (as in docker-compose.e2e.yml); every SELECT is gated.
export AGENT_APPROVAL_ENABLED=true AGENT_ROUTING_ENABLED=true AGENT_SEMANTIC_LAYER_ENABLED=true
export AGENT_STRUCTURED_REPORT_ENABLED=true AGENT_MEMORY_ENABLED=true
export AGENT_MEMORY_DB_PATH="$OUT/data/memory.sqlite3" AGENT_TRACE_STORE_ENABLED=true
export AGENT_TRACES_DIR="$OUT/traces" AGENT_LOG_JSON_ENABLED=true AGENT_LANGFUSE_ENABLED=true
export AGENT_JUDGE_BATCH_ENABLED=true
export AGENT_SAMPLES_ENABLED=true AGENT_SAMPLES_DIR="$ROOT/benchmarks/data"
export AGENT_APPROVAL_ROW_THRESHOLD=0 AGENT_JOB_DB_PATH= AGENT_DATA_DIR="$OUT/data"
export AGENT_ARTIFACTS_DIR="$OUT/art" AGENT_SEMANTIC_METRICS_DIR="$ROOT/benchmarks/metrics"
# The scripted LLM never calls the API; make sure nothing can.
export ANTHROPIC_API_KEY="" ANTHROPIC_BASE_URL="http://127.0.0.1:9"
# Langfuse is on but keyless (the client disables itself): no test traces leave the machine.
export LANGFUSE_PUBLIC_KEY="" LANGFUSE_SECRET_KEY=""

pids=()
cleanup() { for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT

if $DOCKER; then
  compose=(docker compose -p analyst-agent-e2e -f "$ROOT/docker-compose.yml"
    -f "$HERE/docker-compose.e2e.yml")
  docker_cleanup() {
    "${compose[@]}" logs --no-color > "$OUT/compose.log" 2>&1 || true
    "${compose[@]}" down -v --remove-orphans > /dev/null 2>&1 || true
    cleanup
  }
  trap docker_cleanup EXIT
  "${compose[@]}" up -d --build --wait --wait-timeout 180
  # The cross-check reads the approve POSTs from the API log, so follow it into a file.
  "${compose[@]}" logs -f --no-color --no-log-prefix api > "$OUT/api.log" 2>&1 &
  pids+=($!)
  UI="http://127.0.0.1:8501" API="http://127.0.0.1:8000" \
    node "$HERE/ui_e2e.js" "$ROOT/benchmarks/data/saas/subscriptions.csv" "$OUT" "$OUT/api.log"
  exit 0
fi

uv run --no-sync uvicorn --app-dir "$HERE" dryrun_api:app --port "$API_PORT" \
  > "$OUT/api.log" 2>&1 &
pids+=($!)
AGENT_API_URL="http://127.0.0.1:$API_PORT" uv run --no-sync streamlit run frontend/app.py \
  --server.port "$UI_PORT" --server.headless true --browser.gatherUsageStats false \
  > "$OUT/ui.log" 2>&1 &
pids+=($!)

wait_for() {
  for _ in $(seq 1 120); do curl -fsS "$1" > /dev/null 2>&1 && return 0; sleep 0.5; done
  echo "timed out waiting for $1" >&2
  tail -n 50 "$OUT"/*.log >&2
  return 1
}
wait_for "http://127.0.0.1:$API_PORT/healthz"
wait_for "http://127.0.0.1:$UI_PORT/_stcore/health"

UI="http://127.0.0.1:$UI_PORT" API="http://127.0.0.1:$API_PORT" \
  node "$HERE/ui_e2e.js" "$ROOT/benchmarks/data/saas/subscriptions.csv" "$OUT" "$OUT/api.log"
