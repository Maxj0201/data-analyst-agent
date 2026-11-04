# Autonomous Data Analyst Agent

A Python agent that answers natural-language questions over tabular data by inspecting schema and executing SQL/Python in a sandbox.
It self-verifies its answers with a critic step and produces Markdown reports with charts.
Includes an evaluation benchmark for measuring accuracy, robustness, cost, and latency.

## Serving (API + UI)

```bash
uv sync --extra api --extra frontend
uv run uvicorn analyst_agent.api.app:app --reload          # http://localhost:8000/docs
uv run streamlit run frontend/app.py                        # http://localhost:8501
```

Or with Docker: `cp .env.example .env` (add `ANTHROPIC_API_KEY`), then `docker compose up --build`.

| Endpoint                   | Purpose                                                        |
|----------------------------|----------------------------------------------------------------|
| `POST /datasets`           | upload a CSV (multipart `file`) → `dataset_id`                 |
| `POST /ask`                | `{dataset_id, question}` → `job_id` (runs in the background)   |
| `GET /jobs/{id}`           | status, progress, cost, step trace and result                  |
| `GET /jobs/{id}/events`    | Server-Sent Events: plan, LLM/tool calls, critic verdict, final |
| `GET /jobs/{id}/report`    | final report as `text/markdown`                                |
| `GET /artifacts/{path}`    | chart images                                                   |

Deployment: [deploy/cloudrun.md](deploy/cloudrun.md). Tracing: [docs/observability.md](docs/observability.md).
