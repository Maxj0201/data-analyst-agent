#!/usr/bin/env bash
# Run the five benchmark experiments (docs/eval-runbook.md) and write runs/summary.md.
#
#   scripts/run_experiments.sh --agent dry-run --yes        # free end-to-end check, no API calls
#   scripts/run_experiments.sh                              # real models, all 80 tasks, judge on
#   scripts/run_experiments.sh --only semantic --control runs/experiments/<stamp>/baseline-sonnet/<run>
#
# Every arm is one `python -m analyst_agent.eval run` over all tasks with every AGENT_* feature
# flag pinned (a local .env cannot leak into a comparison) and a --budget-usd cap of
# (estimate from core/llm.py's price table) x --cap-factor. The Sonnet baseline is the shared
# control of experiments 2-5 and runs once.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/run_experiments.sh [options]

Options:
  --agent core|dry-run   core = real models (needs ANTHROPIC_API_KEY; the LLM judge grades
                         L3 and ADV). dry-run = scripted offline LLM, no API calls. [core]
  --only EXP             run only this experiment; repeatable. One of: baseline critic semantic
                         routing structured. [all five]
  --control RUN_DIR      reuse an existing Sonnet-baseline run as the control of experiments
                         2-5 instead of running it again
  --cap-factor F         per-arm --budget-usd = estimate x F [1.5]
  --budget-usd USD       fixed per-arm cap instead of the estimate (0 = start no task)
  --max-total-usd USD    stop starting new arms once the total spend reaches this
                         [sum of the per-arm caps]
  --haiku MODEL          [claude-haiku-5-5]
  --sonnet MODEL         [claude-sonnet-5-5]
  --judge-model MODEL    judge for every arm, fixed so arms are comparable [claude-sonnet-5-5]
  --concurrency N        tasks in parallel per arm [4]
  --timeout S            per-task timeout, seconds [600]
  --limit N              stratified sample of N tasks (smoke test) [all 80]
  --out DIR              run directories + manifest [runs/experiments/<UTC stamp>]
  --summary PATH         comparison tables [runs/summary.md]
  --print-commands       print the eval commands for each arm and exit (runs nothing)
  -y, --yes              do not ask for confirmation before spending money
  -h, --help             show this help

Environment: PYTHON (default "uv run python") is the interpreter used for every command.
EOF
}

AGENT=core
ONLY=()
CONTROL_DIR=""
CAP_FACTOR=1.5
FIXED_BUDGET=""
MAX_TOTAL=""
HAIKU=claude-haiku-5-5
SONNET=claude-sonnet-5-5
JUDGE=claude-sonnet-5-5
CONCURRENCY=4
TIMEOUT=600
LIMIT=""
OUT=""
SUMMARY=runs/summary.md
PRINT_ONLY=0
YES=0

die() { echo "error: $*" >&2; exit 2; }
need() { [[ $# -ge 2 && -n "$2" ]] || die "$1 needs a value"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent) need "$@"; AGENT=$2; shift 2 ;;
    --only) need "$@"; ONLY+=("$2"); shift 2 ;;
    --control) need "$@"; CONTROL_DIR=$2; shift 2 ;;
    --cap-factor) need "$@"; CAP_FACTOR=$2; shift 2 ;;
    --budget-usd) need "$@"; FIXED_BUDGET=$2; shift 2 ;;
    --max-total-usd) need "$@"; MAX_TOTAL=$2; shift 2 ;;
    --haiku) need "$@"; HAIKU=$2; shift 2 ;;
    --sonnet) need "$@"; SONNET=$2; shift 2 ;;
    --judge-model) need "$@"; JUDGE=$2; shift 2 ;;
    --concurrency) need "$@"; CONCURRENCY=$2; shift 2 ;;
    --timeout) need "$@"; TIMEOUT=$2; shift 2 ;;
    --limit) need "$@"; LIMIT=$2; shift 2 ;;
    --out) need "$@"; OUT=$2; shift 2 ;;
    --summary) need "$@"; SUMMARY=$2; shift 2 ;;
    --print-commands) PRINT_ONLY=1; shift ;;
    -y|--yes) YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown option $1" ;;
  esac
done

[[ $AGENT == core || $AGENT == dry-run ]] || die "--agent must be core or dry-run"
EXPERIMENTS=(baseline critic semantic routing structured)
for e in "${ONLY[@]}"; do
  [[ " ${EXPERIMENTS[*]} " == *" $e "* ]] || die "unknown experiment '$e'"
done
[[ ${#ONLY[@]} -gt 0 ]] || ONLY=("${EXPERIMENTS[@]}")
selected() { [[ " ${ONLY[*]} " == *" $1 "* ]]; }
if [[ -n $CONTROL_DIR ]]; then
  [[ -f $CONTROL_DIR/results.jsonl ]] || die "--control $CONTROL_DIR has no results.jsonl"
fi

read -r -a PY <<< "${PYTHON:-uv run python}"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT=${OUT:-runs/experiments/$STAMP}

# Pinned for every arm; each arm overrides a few of these.
BASE_ENV=(
  "AGENT_MODEL=$SONNET"
  "AGENT_CRITIC_MODEL="
  "AGENT_MAX_CRITIC_ROUNDS=2"
  "AGENT_ROUTING_ENABLED=false"
  "AGENT_MODEL_L1="
  "AGENT_MODEL_L2="
  "AGENT_MODEL_L3="
  "AGENT_SEMANTIC_LAYER_ENABLED=false"
  "AGENT_SEMANTIC_BACKEND=bm25"
  "AGENT_STRUCTURED_REPORT_ENABLED=false"
  "AGENT_MEMORY_ENABLED=false"
  "AGENT_APPROVAL_ENABLED=false"
  "AGENT_JUDGE_MODEL=$JUDGE"
)

# ---- the plan: experiment | arm | role | config label | env overrides (space-separated)
ARMS=()
add() { ARMS+=("$1|$2|$3|$4|$5"); }
if selected baseline; then
  add baseline haiku treatment "Haiku, all features off" "AGENT_MODEL=$HAIKU"
fi
NEED_CONTROL=0
for e in baseline critic semantic routing structured; do selected "$e" && NEED_CONTROL=1; done
if [[ $NEED_CONTROL == 1 && -z $CONTROL_DIR ]]; then
  add baseline sonnet control "Sonnet, all features off" ""
fi
if selected critic; then
  add critic critic-off treatment "AGENT_MAX_CRITIC_ROUNDS=0" "AGENT_MAX_CRITIC_ROUNDS=0"
fi
if selected semantic; then
  for b in bm25 embedding hybrid; do
    add semantic "semantic-$b" treatment "semantic layer, $b" \
      "AGENT_SEMANTIC_LAYER_ENABLED=true AGENT_SEMANTIC_BACKEND=$b"
  done
fi
if selected routing; then
  add routing routing-on treatment "routing: L1 Haiku, L2/L3 Sonnet" \
    "AGENT_ROUTING_ENABLED=true AGENT_MODEL_L1=$HAIKU AGENT_MODEL_L2=$SONNET AGENT_MODEL_L3=$SONNET"
fi
if selected structured; then
  add structured structured-on treatment "structured report" "AGENT_STRUCTURED_REPORT_ENABLED=true"
fi

arm_env() {  # $1 = overrides; prints BASE_ENV with the overrides applied, one per line
  local base kv out
  for base in "${BASE_ENV[@]}"; do
    out=$base
    for kv in $1; do [[ ${kv%%=*} == "${base%%=*}" ]] && out=$kv; done
    printf '%s\n' "$out"
  done
}

flag_for() {  # eval run's explicit flags, matching the env (recorded in meta.json)
  local overrides=$1 flags=()
  if [[ " $overrides " == *" AGENT_ROUTING_ENABLED=true "* ]]; then flags+=(--routing); else flags+=(--no-routing); fi
  if [[ " $overrides " == *" AGENT_SEMANTIC_LAYER_ENABLED=true "* ]]; then
    flags+=(--semantic-layer)
  else
    flags+=(--no-semantic-layer)
  fi
  printf '%s\n' "${flags[@]}"
}

LIMIT_ARGS=()
[[ -n $LIMIT ]] && LIMIT_ARGS=(--limit "$LIMIT")

estimate_cap() {  # $1 = overrides
  if [[ -n $FIXED_BUDGET ]]; then echo "$FIXED_BUDGET"; return; fi
  local env_lines
  mapfile -t env_lines < <(arm_env "$1")
  env "${env_lines[@]}" "${PY[@]}" -m analyst_agent.eval.experiments estimate \
    --format usd --factor "$CAP_FACTOR" "${LIMIT_ARGS[@]}"
}

eval_cmd() {  # $1 = exp-arm, $2 = overrides, $3 = cap -> prints the command, one arg per line
  local flags
  mapfile -t flags < <(flag_for "$2")
  printf '%s\n' "${PY[@]}" -m analyst_agent.eval run --agent "$AGENT" \
    --judge-model "$JUDGE" --concurrency "$CONCURRENCY" --timeout "$TIMEOUT" \
    --budget-usd "$3" "${flags[@]}" "${LIMIT_ARGS[@]}" --runs-dir "$OUT/$1"
}

# ---- pre-flight
if [[ $AGENT == core ]]; then
  [[ -n ${ANTHROPIC_API_KEY:-} ]] || die "ANTHROPIC_API_KEY is not set: --agent core needs it \
(and the L3 judge would be skipped). Use --agent dry-run for a free check."
  if selected semantic && ! "${PY[@]}" -c "import sentence_transformers" 2>/dev/null; then
    die "the embedding / hybrid arms need the optional extra: uv sync --extra embeddings"
  fi
elif selected semantic && ! "${PY[@]}" -c "import sentence_transformers" 2>/dev/null; then
  echo "warning: sentence-transformers is not installed; in this dry run the embedding/hybrid" \
    "arms record a retrieval error and continue without definitions" >&2
fi

echo "Plan ($AGENT agent, ${LIMIT:-all} tasks, judge $JUDGE, out $OUT):"
CAPS=()
TOTAL_CAP=0
for spec in "${ARMS[@]}"; do
  IFS='|' read -r exp arm role label overrides <<< "$spec"
  cap=$(estimate_cap "$overrides")
  CAPS+=("$cap")
  TOTAL_CAP=$(awk -v a="$TOTAL_CAP" -v b="$cap" 'BEGIN { printf "%.2f", a + b }')
  printf '  %-11s %-19s %-9s cap $%-8s %s\n' "$exp" "$arm" "$role" "$cap" "$label"
done
[[ -n $CONTROL_DIR ]] && echo "  control: reusing $CONTROL_DIR"
MAX_TOTAL=${MAX_TOTAL:-$TOTAL_CAP}
echo "  sum of caps \$$TOTAL_CAP; stop starting arms at \$$MAX_TOTAL"

if [[ $PRINT_ONLY == 1 ]]; then
  for i in "${!ARMS[@]}"; do
    IFS='|' read -r exp arm role label overrides <<< "${ARMS[$i]}"
    mapfile -t cmd < <(eval_cmd "$arm" "$overrides" "${CAPS[$i]}")
    echo
    echo "# $exp / $arm ($label)"
    printf '%s \\\n' "$(arm_env "$overrides" | tr '\n' ' ')"
    printf '  %s\n' "${cmd[*]}"
  done
  exit 0
fi

if [[ $AGENT == core && $YES != 1 ]]; then
  read -r -p "Spend up to \$$MAX_TOTAL on real API calls? [y/N] " ok
  [[ $ok == y || $ok == Y ]] || { echo "aborted"; exit 1; }
fi

# ---- run
mkdir -p "$OUT"
MANIFEST=$OUT/manifest.tsv
printf 'experiment\tarm\trole\trun_dir\tconfig\n' > "$MANIFEST"
SPENT=0
FAILED=()
CONTROL_RUN=$CONTROL_DIR

latest_run() { ls -1d "$1"/*/ 2>/dev/null | sort | tail -n 1 | sed 's:/$::'; }
run_spend() {
  "${PY[@]}" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("spend_usd", 0))' \
    "$1/meta.json"
}
not_started() {  # tasks the arm's --budget-usd cap kept from starting
  "${PY[@]}" -c 'import json,sys
print((json.load(open(sys.argv[1])).get("budget") or {}).get("tasks_not_started", 0))' \
    "$1/meta.json"
}

for i in "${!ARMS[@]}"; do
  IFS='|' read -r exp arm role label overrides <<< "${ARMS[$i]}"
  if awk -v s="$SPENT" -v m="$MAX_TOTAL" 'BEGIN { exit !(s > 0 && s >= m) }'; then
    echo "== skip $exp/$arm: total spend \$$SPENT reached --max-total-usd \$$MAX_TOTAL" >&2
    FAILED+=("$exp/$arm (not started: total budget)")
    continue
  fi
  echo
  echo "== $exp / $arm ($label), cap \$${CAPS[$i]}"
  mapfile -t env_lines < <(arm_env "$overrides")
  mapfile -t cmd < <(eval_cmd "$arm" "$overrides" "${CAPS[$i]}")
  mkdir -p "$OUT/$arm"
  rc=0
  env "${env_lines[@]}" "${cmd[@]}" > "$OUT/$arm.log" 2>&1 || rc=$?
  [[ $rc == 0 ]] || FAILED+=("$exp/$arm (exit $rc; see $OUT/$arm.log)")
  grep -E '^\| (Accuracy \(scored\)|Not started|Errors / timeouts|Total cost) ' "$OUT/$arm.log" \
    | sed 's/^/   /' || true
  run=$(latest_run "$OUT/$arm")
  if [[ -z $run || ! -f $run/results.jsonl ]]; then
    FAILED+=("$exp/$arm (no results)")
    continue
  fi
  spend=$(run_spend "$run")
  SPENT=$(awk -v a="$SPENT" -v b="$spend" 'BEGIN { printf "%.4f", a + b }')
  echo "   -> $run (spent \$$spend, total \$$SPENT)"
  skipped=$(not_started "$run")
  [[ $skipped == 0 ]] || FAILED+=("$exp/$arm (cap hit: $skipped tasks not started)")
  printf '%s\t%s\t%s\t%s\t%s\n' "$exp" "$arm" "$role" "$run" "$label" >> "$MANIFEST"
  [[ $exp == baseline && $arm == sonnet ]] && CONTROL_RUN=$run
done

# The shared control appears in each selected experiment's table.
if [[ -n $CONTROL_RUN ]]; then
  for e in critic semantic routing structured; do
    selected "$e" || continue
    printf '%s\t%s\t%s\t%s\t%s\n' "$e" sonnet control "$CONTROL_RUN" "Sonnet, all features off" \
      >> "$MANIFEST"
  done
  if selected baseline && [[ -n $CONTROL_DIR ]]; then
    printf '%s\t%s\t%s\t%s\t%s\n' baseline sonnet control "$CONTROL_RUN" \
      "Sonnet, all features off" >> "$MANIFEST"
  fi
fi

echo
"${PY[@]}" -m analyst_agent.eval.experiments summary "$MANIFEST" --out "$SUMMARY" \
  --agent "$AGENT" > /dev/null
cp "$SUMMARY" "$OUT/summary.md"
echo "summary: $SUMMARY (copy in $OUT/summary.md); manifest: $MANIFEST; total spend \$$SPENT"
if [[ ${#FAILED[@]} -gt 0 ]]; then
  printf 'incomplete arms:\n' >&2
  printf '  %s\n' "${FAILED[@]}" >&2
  exit 1
fi
