# UI end-to-end check (approval on)

Drives the Streamlit UI in headless Chromium against the real API, `AgentLoop`, sandbox and
`ApprovalBroker`. Only the LLM is scripted (`dry_run_llm`), so no API key is needed. Not part of
`pytest` (it needs a browser); run it after changing `frontend/app.py`, `api/` or `core/approval.py`.

```bash
export AGENT_APPROVAL_ENABLED=true AGENT_ROUTING_ENABLED=true AGENT_SEMANTIC_LAYER_ENABLED=true
export AGENT_APPROVAL_ROW_THRESHOLD=0 AGENT_JOB_DB_PATH= AGENT_DATA_DIR=/tmp/e2e/data AGENT_ARTIFACTS_DIR=/tmp/e2e/art
uv run uvicorn --app-dir scripts/ui-e2e dryrun_api:app --port 8100 > /tmp/e2e/api.log 2>&1 &
AGENT_API_URL=http://127.0.0.1:8100 uv run streamlit run frontend/app.py --server.port 8601 --server.headless true &
node scripts/ui-e2e/ui_e2e.js "$PWD/benchmarks/data/saas/subscriptions.csv" /tmp/e2e /tmp/e2e/api.log
```

Expected output: the approve run shows the report, both runs show the approval line in the step
log, and the API reports `approved: true` / `false` with `decided_by: human` and the deny note.
After a denial the scripted LLM still finishes its pre-planned chart and report, so the report text
is the same in both runs; the denied `run_sql` shows up as an error tool result.

Against the Docker stack, override the API command to `uvicorn --app-dir /harness dryrun_api:app`
with this directory mounted at `/harness`, and set `UI=http://127.0.0.1:8501 API=http://127.0.0.1:8000`.
