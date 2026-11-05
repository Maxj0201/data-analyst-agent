#!/usr/bin/env bash
# Sandbox-escape probes (docs/review.md H1/H2) inside the real API image, two ways:
#
# 1. Through the API: POST /ask -> AgentLoop -> per-job sandbox -> run_python, with a scripted
#    LLM (probe_api.py), so no API key is needed.
#      baseline      run_python works (no SandboxSetupError)
#      h1_fork_exec  _posixsubprocess.fork_exec -> "Operation not permitted", no marker file
#      h2_read       pyarrow.input_stream('/etc/hostname') -> blocked, no file contents
#      h2_write      to_parquet('/app/data/...') -> blocked, no file
# 2. Kernel layers alone (kernel_check.py via `docker exec -u app`): Landlock + seccomp with no
#    audit hook must deny reads/writes outside the roots, fork, exec and sockets.
#
#   scripts/sandbox-probes/run.sh [image] [extra docker run args...]
# e.g. scripts/sandbox-probes/run.sh analyst-agent-api:local --security-opt no-new-privileges
# EXPECT_LANDLOCK=false: the host kernel (or runtime) has no Landlock. The run then expects the
# "Landlock unavailable" warning, and only the API probes (where the audit hook still applies)
# must be blocked.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="${1:-analyst-agent-api:local}"
shift || true
PORT="${PROBE_PORT:-8090}"
EXPECT_LANDLOCK="${EXPECT_LANDLOCK:-true}"
NAME="analyst-agent-probe-$$"

cleanup() { docker rm -f "$NAME" > /dev/null 2>&1 || true; }
trap cleanup EXIT

docker run -d --name "$NAME" -p "127.0.0.1:$PORT:8000" -v "$HERE:/harness:ro" \
  -e AGENT_SAMPLES_ENABLED=true -e AGENT_JOB_DB_PATH=/app/data/jobs.sqlite3 \
  -e ANTHROPIC_API_KEY= -e ANTHROPIC_BASE_URL=http://127.0.0.1:9 \
  "$@" "$IMAGE" uvicorn --app-dir /harness probe_api:app --host 0.0.0.0 --port 8000 > /dev/null

API="http://127.0.0.1:$PORT"
for _ in $(seq 1 120); do curl -fsS "$API/healthz" > /dev/null 2>&1 && break; sleep 0.5; done
curl -fsS "$API/healthz" > /dev/null || { docker logs "$NAME"; exit 1; }
DATASET=$(curl -fsS "$API/datasets/samples" |
  python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["dataset_id"])')

fail=0
report() { if [[ "$1" == 0 ]]; then echo "ok - $2"; else echo "not ok - $2"; fail=1; fi; }

probe() {  # probe <name> <python predicate over r (run_python tool_result) and s (its summary)>
  local job body rc=0
  job=$(curl -fsS -X POST "$API/ask" -H 'content-type: application/json' \
    -d "{\"dataset_id\": \"$DATASET\", \"question\": \"$1\"}" |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')
  for _ in $(seq 1 240); do
    body=$(curl -fsS "$API/jobs/$job")
    grep -q '"status":"\(succeeded\|failed\|cancelled\)"' <<< "$body" && break
    sleep 0.5
  done
  PRED="$2" python3 -c '
import json, os, sys
job = json.load(sys.stdin)
rs = [e["data"] for e in job["trace"]
      if e["type"] == "tool_result" and e["data"].get("name") == "run_python"]
r = rs[0] if rs else {}
s = r.get("summary", "")
print("    " + s.splitlines()[0][:160] if s else "    (no run_python result)")
sys.exit(0 if rs and eval(os.environ["PRED"]) else 1)
' <<< "$body" || rc=$?
  report "$rc" "api: $1"
}

hostname=$(docker exec "$NAME" cat /etc/hostname)
probe baseline 'r["ok"] is True and "rows 1500" in s'
probe h1_fork_exec 'r["ok"] is False and "Operation not permitted" in s'
probe h2_read "r['ok'] is False and '$hostname' not in s"
probe h2_write 'r["ok"] is False'
for f in /app/data/h1_pwned /app/data/h2_escaped.parquet; do
  rc=0; docker exec "$NAME" test ! -e "$f" || rc=$?
  report "$rc" "api: no $f"
done

echo "kernel check (Landlock + seccomp, no audit hook):"
kc=$(docker exec -u app "$NAME" python /harness/kernel_check.py)
echo "    $kc"
KC="$kc" EXPECT_LANDLOCK="$EXPECT_LANDLOCK" python3 - <<'PY' || fail=1
import json, os
kc = json.loads(os.environ["KC"])
landlock = os.environ["EXPECT_LANDLOCK"] == "true"
checks = {
    "seccomp: fork denied": kc["fork"].startswith("PermissionError"),
    "seccomp: execv denied": kc["execv"].startswith("PermissionError"),
    "seccomp: AF_INET socket denied": kc["socket_inet"].startswith("PermissionError"),
    "seccomp: AF_UNIX socket denied": kc["socket_unix"].startswith("PermissionError"),
    "work dir writable": kc["write_inside_workdir"] == "allowed",
}
if landlock:
    checks |= {
        f"Landlock ABI >= 1 (got {kc['landlock_abi']})": kc["landlock_abi"] >= 1,
        "no confinement warnings": kc["warnings"] == [],
        "Landlock: /etc/hostname read denied": kc["read_etc_hostname"] != "allowed",
        "Landlock: job db read denied": kc["read_job_db"] != "allowed",
        "Landlock: data volume write denied": kc["write_data_volume"] != "allowed",
    }
else:
    checks["Landlock unavailable warning"] = any("Landlock unavailable" in w for w in kc["warnings"])
bad = 0
for name, ok in checks.items():
    print(("ok - " if ok else "not ok - ") + "kernel: " + name)
    bad |= not ok
raise SystemExit(bad)
PY
exit "$fail"
