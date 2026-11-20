#!/usr/bin/env bash
# Follow-up to the 2026-11-19 re-run (runs/experiments/rerun-20261119, the published control):
#
#   (c) judge-calibrate --judge-samples 3: the real judge against the 24 hand-labelled reports.
#   (b) critic-off, routing-on and structured-on: fresh full runs with --judge-samples 3, so
#       their L3 deltas against the 3-sample control are like-for-like.
#   (a) The 16 ADV tasks with the PII output guard on (AGENT_PII_GUARD_ENABLED=true), else the
#       control config (scripts/run_experiments.sh --only adv).
#   Then, offline: a summary of the published manifest with these runs replacing the old
#   critic-off / routing-on / structured-on rows, plus the new adv table.
#
#   scripts/rerun-20261120.sh             # asks for the API key (not echoed), then runs it all
#   scripts/rerun-20261120.sh --dry-run   # free: dry-run agent on 8 tasks per arm, fake judge,
#                                         # no key, writes under runs/experiments/rerun-20261120-dry-run
#
# Writes $R/summary.md and $R/manifest.tsv. runs/summary.md and the committed copy the README
# links (docs/results/2026-11-19-summary.md) are left as published; the script prints the
# commands that publish the new summary.
# Environment: CONTROL, R, PYTHON (default "uv run python"), NO_CAFFEINATE=1.
set -euo pipefail

DRY_RUN=0
case "${1:-}" in
  --dry-run) DRY_RUN=1 ;;
  -h|--help) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  "") ;;
  *) echo "usage: $0 [--dry-run]" >&2; exit 2 ;;
esac

cd "$(dirname "$0")/.."

# Keep the Mac awake for the whole run; the inner run_experiments.sh calls then skip their own.
if [[ $DRY_RUN == 0 && $(uname -s) == Darwin && -z ${RERUN_CAFFEINATED:-} && -z ${NO_CAFFEINATE:-} ]] \
  && command -v caffeinate >/dev/null 2>&1; then
  RERUN_CAFFEINATED=1 exec caffeinate -i bash "$0" "$@"
fi

CONTROL=${CONTROL:-runs/experiments/rerun-20261119/sonnet/20261120T021738Z}
PUBLISHED=runs/experiments/rerun-20261119/manifest.tsv
if [[ $DRY_RUN == 1 ]]; then
  R=${R:-runs/experiments/rerun-20261120-dry-run}
  AGENT_ARGS=(--agent dry-run --limit 8 --yes)
  CAL_ARGS=(--fake)
else
  R=${R:-runs/experiments/rerun-20261120}
  AGENT_ARGS=(--agent core --yes)
  CAL_ARGS=()
fi
read -r -a PY <<< "${PYTHON:-uv run python}"
for f in "$CONTROL/meta.json" "$PUBLISHED"; do
  [[ -e $f ]] || { echo "error: $f not found" >&2; exit 2; }
done
[[ ! -e $R/manifest.tsv ]] || { echo "error: $R already holds a finished run" >&2; exit 2; }

API_KEY=""
if [[ $DRY_RUN == 0 ]]; then
  if [[ ! -t 0 ]]; then  # e.g. Claude Code's `!` prefix: read would get EOF and exit silently
    echo "error: no terminal to read the API key from; run this in your own terminal" >&2
    exit 2
  fi
  read -rsp "Anthropic API key (not echoed): " API_KEY || { echo; echo "error: no key" >&2; exit 2; }
  echo
  [[ -n $API_KEY ]] || { echo "error: empty key" >&2; exit 2; }
fi
mkdir -p "$R"

FAILED=()
# with_key CMD...: the key goes to this one command's environment; nothing is exported.
with_key() { ANTHROPIC_API_KEY=$API_KEY RUN_EXPERIMENTS_CAFFEINATED=1 "$@"; }

# (c) Judge calibration (~$0.5): 24 labelled reports x 3 judge calls.
echo
echo "== judge-calibrate --judge-samples 3"
with_key "${PY[@]}" -m analyst_agent.eval judge-calibrate --judge-samples 3 \
  --judge-model claude-sonnet-5-5 ${CAL_ARGS[@]+"${CAL_ARGS[@]}"} \
  --out "$R/judge-calibration.md" --json "$R/judge-calibration.json" > "$R/judge-calibration.log" 2>&1 \
  || FAILED+=("judge-calibrate (see $R/judge-calibration.log)")
grep -iE 'agreement|kappa|κ' "$R/judge-calibration.md" 2>/dev/null | head -n 5 || true

# (b) critic-off, routing-on, structured-on: full, 3 judge samples, against the published control.
#     Caps: the script's estimate x 1.5 ($5.04 / $5.66 / $7.31; the 2026-11-14 runs cost
#     $2.92 / $3.98 / $4.13 with 1 judge sample).
echo
echo "== critic-off, routing-on, structured-on (full, --judge-samples 3)"
with_key scripts/run_experiments.sh "${AGENT_ARGS[@]}" --batch --judge-samples 3 \
  --only critic --only routing --only structured --control "$CONTROL" \
  --out "$R/full" --summary "$R/full/summary.md" \
  || FAILED+=("full re-runs (see $R/full/*.log)")

# (a) ADV only, PII guard on. Fixed cap: the control's 16 ADV tasks cost $0.57 in agent calls,
#     above the estimate-based cap ($0.51).
echo
echo "== pii-guard-on (16 ADV tasks)"
with_key scripts/run_experiments.sh "${AGENT_ARGS[@]}" --batch --budget-usd 1.50 \
  --only adv --control "$CONTROL" --out "$R/adv" --summary "$R/adv/summary.md" \
  || FAILED+=("adv arm (see $R/adv/*.log)")

# Offline: published manifest, then the new rows (a later row replaces an earlier one for the
# same experiment and arm), written out as one manifest.
echo
echo "== summary $R/summary.md"
{
  cat "$PUBLISHED"
  for m in "$R/full/manifest.tsv" "$R/adv/manifest.tsv"; do
    [[ -f $m ]] && tail -n +2 "$m"
  done
} > "$R/manifest.all.tsv"
ANTHROPIC_API_KEY= "${PY[@]}" - "$R/manifest.all.tsv" "$R/manifest.tsv" <<'EOF'
import csv, sys
rows = {}
with open(sys.argv[1], newline="") as f:
    reader = csv.DictReader(f, delimiter="\t")
    for rec in reader:
        rows[(rec["experiment"], rec["arm"])] = rec
with open(sys.argv[2], "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=reader.fieldnames, delimiter="\t", lineterminator="\n")
    w.writeheader()
    w.writerows(rows.values())
EOF
cat "$R/manifest.tsv"
if ANTHROPIC_API_KEY= "${PY[@]}" -m analyst_agent.eval.experiments summary "$R/manifest.tsv" \
  --out "$R/summary.md" --agent core > /dev/null; then
  {
    echo
    echo "_Re-run 2026-11-20 (scripts/rerun-20261120.sh): critic-off, routing-on and structured-on"
    echo "re-run with 3 L3 judge samples like the control; pii-guard-on ran the 16 ADV tasks only"
    echo "and is compared with the control's same 16 tasks. haiku and semantic-embedding are still"
    echo "the 2026-11-14 runs with 1 judge sample. Judge calibration: $R/judge-calibration.md._"
  } >> "$R/summary.md"
  echo "summary: $R/summary.md"
  echo "to publish it: cp $R/summary.md runs/summary.md; cp $R/summary.md docs/results/2026-11-20-summary.md;"
  echo "  cp $R/manifest.tsv docs/results/2026-11-20-manifest.tsv; then update the README Results table"
else
  FAILED+=("summary")
fi

if [[ ${#FAILED[@]} -gt 0 ]]; then
  printf 'failed steps:\n' >&2
  printf '  %s\n' "${FAILED[@]}" >&2
  exit 1
fi
