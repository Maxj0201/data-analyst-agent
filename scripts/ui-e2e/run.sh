#!/usr/bin/env bash
# Starts the dry-run API (real API + AgentLoop + sandbox + ApprovalBroker, scripted LLM) and the
# Streamlit UI, then drives the UI in headless Chromium (ui_e2e.js). Used by CI (ui-e2e job) and
# locally:  scripts/ui-e2e/run.sh [out_dir]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HERE="$ROOT/scripts/ui-e2e"
OUT="${1:-${TMPDIR:-/tmp}/ui-e2e}"
API_PORT="${API_PORT:-8100}"
UI_PORT="${UI_PORT:-8601}"
mkdir -p "$OUT/data" "$OUT/art"
cd "$ROOT"

# Every feature flag the UI renders is on; every SELECT is gated (threshold 0).
export AGENT_APPROVAL_ENABLED=true AGENT_ROUTING_ENABLED=true AGENT_SEMANTIC_LAYER_ENABLED=true
export AGENT_SAMPLES_ENABLED=true AGENT_SAMPLES_DIR="$ROOT/benchmarks/data"
export AGENT_APPROVAL_ROW_THRESHOLD=0 AGENT_JOB_DB_PATH= AGENT_DATA_DIR="$OUT/data"
export AGENT_ARTIFACTS_DIR="$OUT/art" AGENT_SEMANTIC_METRICS_DIR="$ROOT/benchmarks/metrics"
# The scripted LLM never calls the API; make sure nothing can.
export ANTHROPIC_API_KEY="" ANTHROPIC_BASE_URL="http://127.0.0.1:9"

pids=()
cleanup() { for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT

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
