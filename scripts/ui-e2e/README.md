# UI end-to-end check

Drives the Streamlit UI in headless Chromium against the real API, `AgentLoop`, sandbox and
`ApprovalBroker`, with routing, the semantic layer, approval (every `SELECT` gated) and the bundled
samples all on. Only the LLM is scripted (`dry_run_llm` via `dryrun_api.py`), so no API key is
needed. CI runs it on every PR that touches `frontend/`, `src/analyst_agent/api/`,
`core/approval.py`, `core/fake_llm.py` or this directory (`.github/workflows/ui-e2e.yml`).

```bash
scripts/ui-e2e/run.sh /tmp/ui-e2e
```

`run.sh` starts the dry-run API on `:8100` and Streamlit on `:8601` (override with `API_PORT` /
`UI_PORT`), waits for both, runs `ui_e2e.js` and stops them. It needs `uv sync --all-extras` and
Node with `playwright` resolvable (CI installs it into this directory).

Two runs, each check printed as `ok - …`, first failure exits non-zero:

1. **approve**: the preselected sample + an example question from the sidebar → the timeline
   streams route / metric definitions / plan / tool calls / approval required → **Approve** →
   approval decision, critic verdict, report with key numbers and an inline chart, the
   *Cost & tokens* tab and the *Raw trace* tab.
2. **deny**: upload `subscriptions.csv` → type a question → **Deny** with a note → the denial and
   note appear in the timeline. The scripted LLM still finishes its pre-planned chart and report;
   the denied `run_sql` shows up as an error tool result.

Finally both jobs are cross-checked through `GET /jobs/{id}`: `approved: true` / `false`,
`decided_by: human` and the deny note. Screenshots (`*.png`) and server logs land in the output
directory (uploaded as a CI artifact).

Against the Docker stack, override the API command to `uvicorn --app-dir /harness dryrun_api:app`
with this directory mounted at `/harness`, and run
`UI=http://127.0.0.1:8501 API=http://127.0.0.1:8000 node scripts/ui-e2e/ui_e2e.js <csv> <out> [api log]`.
