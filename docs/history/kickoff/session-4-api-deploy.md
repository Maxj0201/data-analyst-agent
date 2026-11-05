Read CLAUDE.md first. Scope: src/analyst_agent/api/, Dockerfile, deploy/, frontend/. Branch: feat/api-deploy.

Build the serving layer:
1. api/app.py — FastAPI with: POST /datasets (upload CSV, returns dataset_id, stored under data/uploads/), POST /ask {dataset_id, question} returning job_id, GET /jobs/{id} with status/progress/trace/result, GET /jobs/{id}/report (Markdown), GET /artifacts/{path} for chart PNGs. Jobs run as background tasks; in-memory job store with SQLite fallback.
2. SSE endpoint GET /jobs/{id}/events emitting each agent step (plan, tool call, tool result summary, critic verdict) as it happens.
3. frontend/ — minimal Streamlit app: upload CSV, ask question, watch the live step stream, render final report and charts, show cost. Simple and clean.
4. Dockerfile (multi-stage, uv, non-root user) and docker-compose.yml running api + frontend. deploy/cloudrun.md with concrete commands to deploy to Google Cloud Run and set AGENT_* env vars from secrets.
5. Observability: Langfuse via its Python SDK behind a feature flag (AGENT_LANGFUSE_ENABLED); every LLM call and tool call becomes a span. docs/observability.md on self-hosting Langfuse with docker.
6. Tests for the API using httpx with the agent loop mocked.
Open a PR when done.
