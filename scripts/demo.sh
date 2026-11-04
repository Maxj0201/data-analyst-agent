#!/usr/bin/env bash
# Build the images, start the API + Streamlit UI with docker compose (bundled sample datasets
# on), wait until both are healthy, and print the URL.
#
#   scripts/demo.sh            # build + start (detached)
#   scripts/demo.sh --down     # stop and remove the containers (volumes are kept)
#
# Feature flags pass through from .env or the shell, e.g.
#   AGENT_ROUTING_ENABLED=true AGENT_APPROVAL_ENABLED=true scripts/demo.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if ! command -v docker > /dev/null 2>&1; then
  echo "demo: docker is not installed (https://docs.docker.com/get-docker/)" >&2
  exit 1
fi
if ! docker compose version > /dev/null 2>&1; then
  echo "demo: the docker compose plugin is required (docker compose, not docker-compose)" >&2
  exit 1
fi
if ! docker info > /dev/null 2>&1; then
  echo "demo: cannot reach the Docker daemon; is it running?" >&2
  exit 1
fi

if [[ "${1:-}" == "--down" ]]; then
  docker compose down
  exit 0
fi

API_URL="http://localhost:8000"
UI_URL="http://localhost:8501"

key_set=false
if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
  key_set=true
elif [[ -f .env ]] && grep -Eq '^ANTHROPIC_API_KEY=.+' .env; then
  key_set=true
fi
if [[ "$key_set" != true ]]; then
  echo "demo: ANTHROPIC_API_KEY is not set (in .env or the shell). The UI will start, but" >&2
  echo "      questions will fail at the first LLM call. cp .env.example .env and add the key." >&2
fi

export AGENT_SAMPLES_ENABLED=true
echo "demo: building images and starting the stack (first build takes a few minutes)..."
docker compose up --build --detach

wait_for() {
  local url="$1" name="$2"
  for _ in $(seq 1 120); do
    if curl -fsS "$url" > /dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "demo: $name did not become healthy at $url; recent logs:" >&2
  docker compose logs --tail 50 >&2
  return 1
}
wait_for "$API_URL/healthz" "the API"
wait_for "$UI_URL/_stcore/health" "the UI"

cat <<MSG

  Analyst agent is up.

    UI:        $UI_URL   (pick a sample dataset and an example question in the sidebar)
    API docs:  $API_URL/docs

  Logs:  docker compose logs -f
  Stop:  scripts/demo.sh --down

MSG
