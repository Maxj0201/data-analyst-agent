#!/usr/bin/env bash
# Pre-flight for the first real-API benchmark run (docs/eval-runbook.md, "Tomorrow's run").
#
#   scripts/preflight.sh                 # real checks: ~3 tiny API calls, well under $0.01
#   scripts/preflight.sh --limit 8       # cost estimate for the 8-task smoke run instead
#   scripts/preflight.sh --dry-run       # rehearsal: the API checks hit the scripted FakeLLM
#
# Checks: API key set and accepted (one minimal Messages call), every model the runbook uses
# answers a 1-token call (and the judge returns parseable structured output), every optional
# extra is installed, the benchmark regenerates byte-for-byte, the metric definitions load,
# the trace store / runs / experiments directories are writable; then prints the estimated
# cost of each experiment from core/llm.py's price table. Exits 1 if any check fails.
# A missing [embeddings] extra (torch) is a WARN, not a FAIL, unless you pass --embeddings or
# AGENT_SEMANTIC_BACKEND is embedding/hybrid: pass it before running the semantic experiment.
#
# Options are passed to `python -m analyst_agent.eval.preflight` (see --help there):
#   --judge-model M  --limit N  --cap-factor F  --batch  --dry-run  --skip-api
#   --skip-regenerate  --embeddings  --experiments-dir DIR
# Environment: PYTHON (default "uv run python") is the interpreter.
set -euo pipefail

if [[ ${1:-} == -h || ${1:-} == --help ]]; then
  sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi

cd "$(dirname "$0")/.."
read -r -a PY <<< "${PYTHON:-uv run python}"

if ! "${PY[@]}" -c "import analyst_agent" 2>/dev/null; then
  echo "error: cannot import analyst_agent with '${PY[*]}': run uv sync --locked --all-extras" >&2
  exit 1
fi

exec "${PY[@]}" -m analyst_agent.eval.preflight "$@"
