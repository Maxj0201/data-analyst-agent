#!/usr/bin/env bash
# One-off re-run after the 2026-11-14 experiments (runs/experiments/20261114T083237Z):
#
#   1. adv_15 / adv_16 only (API 400 "fallback.from", fixed in #39): resume the original
#      sonnet, routing-on and structured-on runs with --retry-errors (their only non-ok rows).
#   2. Fresh full runs with --judge-samples 3: a new Sonnet control, semantic-bm25 and
#      semantic-hybrid (whose originals lost 8 tasks to a host sleep).
#   3. Then, offline: rescore the 3 resumed runs, write a manifest (sonnet / semantic-bm25 /
#      semantic-hybrid from the new runs, the rest from the original run's rescored/ dirs) and
#      the experiments summary.
#
#   scripts/rerun-20261119.sh             # asks for the API key (not echoed), then runs it all
#   scripts/rerun-20261119.sh --dry-run   # no key, no API calls, no rescore: builds
#                                         # $R/manifest.dry-run.tsv and $R/summary.dry-run.md
#                                         # from whatever runs exist under $R
#
# Environment: B (original experiment dir), R (re-run output dir), SUMMARY (summary path),
# MANIFEST (manifest path), PYTHON (default "uv run python"), NO_CAFFEINATE=1.
# Every arm pins the same AGENT_* environment scripts/run_experiments.sh pinned on 2026-11-14:
# --resume refuses to continue a run whose recorded settings differ.
set -euo pipefail

DRY_RUN=0
case "${1:-}" in
  --dry-run) DRY_RUN=1 ;;
  -h|--help) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  "") ;;
  *) echo "usage: $0 [--dry-run]" >&2; exit 2 ;;
esac

cd "$(dirname "$0")/.."

# Keep the Mac awake for the paid run (an idle sleep cost the 2026-11-14 semantic arms 8 tasks).
if [[ $DRY_RUN == 0 && $(uname -s) == Darwin && -z ${RERUN_CAFFEINATED:-} && -z ${NO_CAFFEINATE:-} ]] \
  && command -v caffeinate >/dev/null 2>&1; then
  RERUN_CAFFEINATED=1 exec caffeinate -i bash "$0" "$@"
fi

B=${B:-runs/experiments/20261114T083237Z}
R=${R:-runs/experiments/rerun-20261119}
if [[ $DRY_RUN == 1 ]]; then  # never overwrite the real summary or manifest
  SUMMARY=${SUMMARY:-$R/summary.dry-run.md}
  MANIFEST=${MANIFEST:-$R/manifest.dry-run.tsv}
else
  SUMMARY=${SUMMARY:-runs/summary.md}
  MANIFEST=${MANIFEST:-$R/manifest.tsv}
fi
read -r -a PY <<< "${PYTHON:-uv run python}"

SONNET_RUN=$B/sonnet/20261114T085028Z
ROUTING_RUN=$B/routing-on/20261114T225256Z
STRUCTURED_RUN=$B/structured-on/20261114T230619Z
for d in "$B/manifest.tsv" "$SONNET_RUN/meta.json" "$ROUTING_RUN/meta.json" "$STRUCTURED_RUN/meta.json"; do
  [[ -e $d ]] || { echo "error: $d not found" >&2; exit 2; }
done

# Pinned for every arm (run_experiments.sh BASE_ENV); each arm overrides a few below.
BASE_ENV=(
  AGENT_MODEL=claude-sonnet-5-5
  AGENT_CRITIC_MODEL=
  AGENT_MAX_CRITIC_ROUNDS=2
  AGENT_ROUTING_ENABLED=false
  AGENT_MODEL_L1=
  AGENT_MODEL_L2=
  AGENT_MODEL_L3=
  AGENT_SEMANTIC_LAYER_ENABLED=false
  AGENT_SEMANTIC_BACKEND=bm25
  AGENT_STRUCTURED_REPORT_ENABLED=false
  AGENT_MEMORY_ENABLED=false
  AGENT_APPROVAL_ENABLED=false
  AGENT_JUDGE_MODEL=claude-sonnet-5-5
)
FULL_ARGS=(--agent core --judge-model claude-sonnet-5-5 --concurrency 4 --timeout 600 --batch
  --judge-samples 3)

FAILED=()
# run_arm LABEL "OVERRIDE=..." ... -- EVAL_RUN_ARGS...
# The key goes to each command's environment only; it is never exported by this script.
run_arm() {
  local label=$1 overrides=() rc=0
  shift
  while [[ $1 != -- ]]; do overrides+=("$1"); shift; done
  shift
  echo
  echo "== $label"
  ANTHROPIC_API_KEY=$API_KEY env "${BASE_ENV[@]}" ${overrides[@]+"${overrides[@]}"} \
    "${PY[@]}" -m analyst_agent.eval run "$@" || rc=$?
  [[ $rc == 0 ]] || FAILED+=("$label (exit $rc)")
}

latest_run() {  # newest run dir under $1, or nothing (never fails, even with no runs)
  { ls -1d "$1"/*/ 2>/dev/null || true; } | sort | tail -n 1 | sed 's:/$::'
}

if [[ $DRY_RUN == 0 ]]; then
  if [[ ! -t 0 ]]; then  # e.g. Claude Code's `!` prefix: read would get EOF and exit silently
    echo "error: no terminal to read the API key from; run this in your own terminal" >&2
    exit 2
  fi
  read -rsp "Anthropic API key (not echoed): " API_KEY || { echo; echo "error: no key" >&2; exit 2; }
  echo
  [[ -n $API_KEY ]] || { echo "error: empty key" >&2; exit 2; }
  mkdir -p "$R"

  # 1. adv_15 / adv_16: retry the errored rows of the original runs (resume keeps their caps
  #    and their recorded --judge-samples 1; ADV is not affected by judge samples).
  run_arm "resume sonnet (adv_15/16)" -- \
    --resume "$SONNET_RUN" --retry-errors --batch --concurrency 4
  run_arm "resume routing-on (adv_15/16)" \
    AGENT_ROUTING_ENABLED=true AGENT_MODEL_L1=claude-haiku-5-5 \
    AGENT_MODEL_L2=claude-sonnet-5-5 AGENT_MODEL_L3=claude-sonnet-5-5 -- \
    --resume "$ROUTING_RUN" --retry-errors --batch --concurrency 4
  run_arm "resume structured-on (adv_15/16)" AGENT_STRUCTURED_REPORT_ENABLED=true -- \
    --resume "$STRUCTURED_RUN" --retry-errors --batch --concurrency 4

  # 2. Fresh full runs, 3 judge samples. Caps: the 2026-11-14 estimates raised for 3x L3 judge.
  run_arm "sonnet control (full, 3 judge samples)" -- \
    "${FULL_ARGS[@]}" --budget-usd 7.50 --no-routing --no-semantic-layer --runs-dir "$R/sonnet"
  for be in bm25 hybrid; do
    run_arm "semantic-$be (full, 3 judge samples)" \
      AGENT_SEMANTIC_LAYER_ENABLED=true "AGENT_SEMANTIC_BACKEND=$be" -- \
      "${FULL_ARGS[@]}" --budget-usd 7.50 --no-routing --semantic-layer \
      --runs-dir "$R/semantic-$be"
  done

  # 3a. Rescore the resumed runs (writes <run>/rescored, overwriting the 2026-11-19 rescore).
  for run in "$SONNET_RUN" "$ROUTING_RUN" "$STRUCTURED_RUN"; do
    echo
    echo "== rescore $run"
    ANTHROPIC_API_KEY= ANTHROPIC_BASE_URL=http://127.0.0.1:9 \
      "${PY[@]}" -m analyst_agent.eval rescore "$run" || FAILED+=("rescore $run")
  done
fi

# 3b. Manifest: sonnet (every experiment's control row), semantic-bm25 and semantic-hybrid from
#     the new runs under $R; every other arm from the original run's rescored/ dir.
build_manifest() {
  local exp arm role run_dir config new
  mkdir -p "$(dirname "$MANIFEST")"
  {
    IFS= read -r header
    printf '%s\n' "$header"
    while IFS=$'\t' read -r exp arm role run_dir config; do
      case $arm in
        sonnet|semantic-bm25|semantic-hybrid)
          new=$(latest_run "$R/$arm")
          [[ -n $new ]] || { echo "error: no run under $R/$arm" >&2; return 1; }
          run_dir=$new ;;
        *)
          [[ -f $run_dir/rescored/results.jsonl ]] \
            || { echo "error: $run_dir has no rescored/ results" >&2; return 1; }
          run_dir=$run_dir/rescored ;;
      esac
      printf '%s\t%s\t%s\t%s\t%s\n' "$exp" "$arm" "$role" "$run_dir" "$config"
    done
  } < "$B/manifest.tsv" > "$MANIFEST"
}

echo
echo "== manifest $MANIFEST"
build_manifest
cat "$MANIFEST"

echo
echo "== summary $SUMMARY"
ANTHROPIC_API_KEY= "${PY[@]}" -m analyst_agent.eval.experiments summary "$MANIFEST" \
  --out "$SUMMARY" --agent core > /dev/null
{
  echo
  echo "_Re-run 2026-11-19 (scripts/rerun-20261119.sh): sonnet, semantic-bm25 and semantic-hybrid"
  echo "are fresh full runs graded with 3 L3 judge samples; critic-off, routing-on and"
  echo "structured-on were graded with 1, so their L3 deltas against this control are not"
  echo "like-for-like. Other arms: $B, re-scored offline._"
} >> "$SUMMARY"
if [[ $DRY_RUN == 0 ]]; then
  cp "$SUMMARY" "$R/summary.md"
  echo "summary: $SUMMARY (copy in $R/summary.md)"
else
  echo "dry run: summary written to $SUMMARY only"
fi

if [[ ${#FAILED[@]} -gt 0 ]]; then
  printf 'failed steps:\n' >&2
  printf '  %s\n' "${FAILED[@]}" >&2
  exit 1
fi
