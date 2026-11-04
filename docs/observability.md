# Observability with Langfuse

The API can send a trace of every job to [Langfuse](https://langfuse.com) through its Python SDK.
Tracing is **off by default** and controlled by one feature flag:

| Variable                 | Meaning                                                    |
|--------------------------|------------------------------------------------------------|
| `AGENT_LANGFUSE_ENABLED` | `true` to turn tracing on (default `false`)                |
| `LANGFUSE_PUBLIC_KEY`    | project public key (`pk-lf-...`)                           |
| `LANGFUSE_SECRET_KEY`    | project secret key (`sk-lf-...`)                           |
| `LANGFUSE_HOST`          | Langfuse URL, e.g. `http://localhost:3000` when self-hosted |

## What gets traced

One trace per job. The root observation is an `agent` span named `analyst-job`, carrying the
question, the dataset id and the job id. Its input is the question and its output is the final
report. Every step of the agent becomes a child observation:

| Agent step                        | Langfuse observation | Contents                                                 |
|-----------------------------------|----------------------|----------------------------------------------------------|
| each LLM call (agent, critic, …)  | `generation` `llm:<role>` | model, token usage incl. cache read/write, USD cost, latency |
| each tool call (`run_sql`, …)     | `tool` `tool:<name>` | tool input → result summary; `ERROR` level if the tool failed |
| plan                              | `span` `plan`        | the agent's plan text                                    |
| critic verdict                    | `evaluator` `critic` | verdict, issues, suggested fix (`WARNING` on `revise`)   |

How it is wired (`src/analyst_agent/api/observability.py`, `src/analyst_agent/api/agent.py`):

* The agent core knows nothing about Langfuse. The API wraps the core's LLM client
  (`EventedLLM`) so each `create()` call, which still goes through `core/llm.py`, emits an
  `llm_call` event. The loop's own `on_event` hook supplies the plan, tool and critic events.
* The same event stream feeds the SSE endpoint, the job trace and Langfuse, so what you see in
  the UI and in Langfuse is always the same sequence.
* Tracing failures are logged and swallowed and never fail a job. If the flag is on but the SDK
  cannot initialise, the API logs an error and runs without tracing.

## Self-hosting Langfuse with Docker

Langfuse v3 runs as a web container and a worker, backed by Postgres, ClickHouse, Redis and
S3-compatible blob storage (MinIO). The official repository ships a compose file for all of it.
It needs about 4 CPU and 16 GB RAM for comfortable use. A laptop is fine for development.

1. Fetch the compose file:

   ```bash
   git clone --depth 1 https://github.com/langfuse/langfuse.git ~/langfuse
   ```

2. **Change the secrets.** Every value marked `# CHANGEME` in `docker-compose.yml` (database
   passwords, `NEXTAUTH_SECRET`, `SALT`, `ENCRYPTION_KEY`, MinIO credentials) must be replaced
   before you expose it anywhere. Generate values with:

   ```bash
   openssl rand -hex 32
   ```

3. Start it:

   ```bash
   cd ~/langfuse && docker compose up -d
   ```

   Wait until `langfuse-web` logs `Ready`, then open <http://localhost:3000>.

4. Sign up (the first user becomes the owner), create an organisation and a project, and
   create API keys under **Project → Settings → API Keys**.

5. Point the agent at it, in `.env` next to `docker-compose.yml`:

   ```bash
   AGENT_LANGFUSE_ENABLED=true
   LANGFUSE_PUBLIC_KEY=pk-lf-...
   LANGFUSE_SECRET_KEY=sk-lf-...
   LANGFUSE_HOST=http://host.docker.internal:3000
   ```

   `host.docker.internal` reaches Langfuse on the host. The `api` service already maps it, on
   Linux too. Alternatively, attach the `api` service to Langfuse's compose network and use
   `http://langfuse-web:3000`. When running the API outside Docker, use
   `http://localhost:3000`.

6. Restart the API and check the flag took effect:

   ```bash
   docker compose up -d api
   ```

   ```bash
   curl -s localhost:8000/healthz
   ```

   It should report `"langfuse": true`. Run a question; the trace appears under **Tracing**
   within a few seconds (the SDK batches and flushes at the end of each job).

### Production notes

* Put Langfuse behind TLS (a reverse proxy or a managed load balancer) and set `NEXTAUTH_URL` to
  its public URL. Disable open sign-up once your team is in (`AUTH_DISABLE_SIGNUP=true`).
* Back up the Postgres and ClickHouse volumes. MinIO holds raw event payloads.
* Traces contain user questions, SQL and report text, and so can contain data from uploaded
  files. Treat the Langfuse instance as holding the same sensitivity as the data itself.
* On Cloud Run, store the keys in Secret Manager like the other variables
  (see [deploy/cloudrun.md](../deploy/cloudrun.md)).
