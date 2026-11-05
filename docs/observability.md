# Observability

Every agent run (an API job, a CLI `ask`, an eval task) produces three kinds of telemetry, each
behind its own flag and all **off by default**:

| Flag | What you get |
|---|---|
| `AGENT_LANGFUSE_ENABLED` | The run's span tree in [Langfuse](https://langfuse.com), live; eval runs also push scores onto each trace. |
| `AGENT_TRACE_STORE_ENABLED` | The same span tree saved as JSON, for `analyst-agent trace show` (no Langfuse needed). |
| `AGENT_LOG_JSON_ENABLED` | One JSON object per log line, with `run_id` / `job_id` / `task_id` correlation. |

Langfuse also needs `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and `LANGFUSE_HOST`, and the
`langfuse` package (the `api` extra). If the SDK cannot initialise, the run continues without it
and an error is logged.

## The span tree

The loop's event hook and `EventedLLM` (one `llm_call` event per completed request, still sent
through `core/llm.py`) describe everything a run does. `core/tracing.py:TraceRecorder` folds that
event stream into one tree per run. The agent core never imports Langfuse.

```
run                                   agent       run_id, job_id/task_id, totals, prompt versions, routed/final tier
├─ route                              chain       tier, model, reason, source        (AGENT_ROUTING_ENABLED)
│  └─ llm:router                      generation
├─ semantic_retrieval                 retriever   metrics, scores                    (AGENT_SEMANTIC_LAYER_ENABLED)
├─ turn 1                             span        turn, model, tier, cost
│  ├─ llm:agent                       generation  model, tokens, cost, latency, prompt + version, tier
│  ├─ plan                            span
│  ├─ tool:run_python                 tool        input → result summary; ERROR level on failure
│  │  └─ approval:run_python          guardrail   reasons, approved, decided_by, wait time (AGENT_APPROVAL_ENABLED)
│  ├─ tool:final_report               tool
│  ├─ critic                          evaluator   verdict, issues; WARNING on revise
│  │  └─ llm:critic                   generation
│  └─ escalation:L1->L2               span        trigger, from/to model             (routing)
├─ turn 2 …
└─ llm:report_writer                  generation  (only when the run hits max_turns)
```

The second column is the Langfuse observation type. A turn starts with the loop's `turn` event,
and everything up to the next turn belongs to it. The router and critic `llm_call`s nest under
their `route` / `critic` spans. An approval gate runs after `tool_call` and before the tool
executes, so it nests under the tool span and the tool's duration includes the human's wait.

Metadata, on the Langfuse observation and in the saved JSON:

* **Cost.** Each generation has its USD cost (`cost_details.total` and `metadata.cost_usd`).
  Containers (turn, critic, route, run) carry the sum of their descendants. The run also carries
  token totals, `llm_calls`, `tool_calls` and the list of models used.
* **Model** on every generation and turn.
* **Prompt version.** Generations record which prompt their role sends (`planner`, `critic`,
  `router`, `report_writer`) and its version (`metadata.prompt_version`, and Langfuse `version`),
  so a prompt change can be compared in Langfuse. The run records every prompt version.
* **Routed tier.** The run has `routed_tier` / `routed_model`, plus `final_tier` after an
  escalation. Each turn and agent/report-writer generation has the tier active at that moment.

Langfuse has no API to back-date a span start, so in Langfuse a generation appears at the moment
its call completed, with the true latency in `metadata.latency_ms`. The saved JSON tree is
back-dated (start = completion − latency), so `trace show` timings are exact.

Tracing never fails a run. The recorder catches sink errors and logs them, and the API's
`RecorderTracer` catches its own errors.

### Where it is wired

| Surface | Root name | Ids on the run span | Saved to (`AGENT_TRACE_STORE_ENABLED`) |
|---|---|---|---|
| API job (`api/jobs.py`, `api/observability.py`) | `analyst-job` | `job_id`, `dataset_id`, `run_id` | `<AGENT_TRACES_DIR>/<job_id>.json` |
| CLI `ask` (`cli.py`) | `analyst-run` | `run_id` | `<AGENT_TRACES_DIR>/<run_id>.json`; `ask --trace FILE` also embeds it under `spans` |
| Eval task (`eval/agent.py`, `eval/runner.py`) | `analyst-eval` | `task_id`, `run_id`, `eval_run`, `difficulty` | `<run_dir>/traces/<run_id>.json` |

The API streams two extra events over SSE for this: `run_start` (`run_id`, model, flags, prompt
versions) and `turn` (`turn`, `model`, `tier`).

## Eval scores in Langfuse

With `AGENT_LANGFUSE_ENABLED`, `eval run` creates each task's Langfuse trace id before the task
starts and passes it in `AgentInput.trace_id`. The core agent records its tree under that id,
inside the isolated child process. Once the task is scored, the runner attaches these scores to
the same trace (`core/langfuse_export.py:eval_scores`):

| Score | Type | When |
|---|---|---|
| `eval_status` | categorical | always: `ok` / `error` / `timeout` |
| `cost_usd` | numeric | always |
| `accuracy` | numeric | graded tasks (the scorer's 0–1 score) |
| `passed` | boolean | graded tasks; `0` for `error` / `timeout` too, so failures show up in a pass/fail filter |
| `judge_score` | numeric | L3 (rubric fraction) and ADV (behaviour judge 0/1) when the judge ran |

Ungraded tasks (`--no-score`, or L3 without a judge) get no `accuracy` / `passed`. In Langfuse,
filter traces by `passed = false` (or `eval_status = timeout`) to open the failing runs.
Each score's comment names the task and its mark, and its metadata carries the task id,
difficulty, tags and eval run directory. `results.jsonl` gains `run_id`, `trace_id` and
`trace_path`, so a row leads to its trace.

A task that times out is killed before it can flush its spans. Its scores still land on the
trace id, so the trace shows up in Langfuse with scores but no spans.

## `analyst-agent trace show`

For people without Langfuse. With `AGENT_TRACE_STORE_ENABLED=true` (or `ask --trace FILE`):

```bash
analyst-agent trace show run_563d6c7512934a43
```

Example output (abridged, illustrative numbers):

```
✓ run run_563d6c7512934a43 status=completed tier=L2                        +   0.00s   14.20s   $0.0912
├─ ✓ route L2 → claude-opus-5-5                                            +   0.03s    310ms   $0.0001
│  └─ ✓ llm:router claude-haiku-5-5 in=412 out=18 prompt=router@1.0.0      +   0.03s    310ms   $0.0001
├─ ✓ turn 1 model=claude-opus-5-5 tier=L2                                  +   0.34s    2.81s   $0.0213
│  ├─ ✓ llm:agent claude-opus-5-5 [L2] in=3,912 out=301 prompt=planner@1.2.0 …
│  ├─ ✓ plan "1. Inspect `payroll`. 2. Aggregate SUM("base_pay") by `pay_p…"
│  └─ ✓ tool:inspect_schema                                                +   2.95s      7ms
…
└─ ✓ turn 4 model=claude-opus-5-5 tier=L2
   ├─ ✓ tool:final_report
   └─ ! critic round=1 verdict=revise                                      …                $0.0080  (revise)
      └─ ✓ llm:critic claude-opus-5-5 …

total 14.20s | $0.0912 | 6 LLM calls | 4 tool calls | models: claude-haiku-5-5, claude-opus-5-5
prompts: critic@1.1.0, memory_rules@1.0.0, planner@1.2.0, report_writer@1.0.0, router@1.0.0, semantic_layer_rules@1.0.0, structured_report_rules@1.0.0
```

Each line shows its status (`✓` ok, `!` warning, `✗` error), its offset from the run start, its
duration and its cost (the subtree total). The argument can be a run id, a job id, an eval task
id or a file path. Ids are looked up under `AGENT_TRACES_DIR` and `AGENT_EVAL_RUNS_DIR` (or
`--dir`, repeatable). `--compact` hides zero-length spans (plans, escalations), and `--json`
prints the raw tree. `analyst-agent trace ls` lists saved traces with status, duration and cost.

## Structured logs

`core/logs.py` uses stdlib `logging` with a context variable. These ids are bound once and then
appear on every record emitted inside that scope, in any module:

* `job_id`: `api/jobs.py`, around each job in its worker thread
* `run_id`: `AgentLoop.run`, around the whole run (the loop, tools and sandbox run in that thread)
* `task_id`: the eval runner and the core eval agents

So the API's `job started` / `job finished`, the loop's `run started` / `tool call` /
`run finished` and the sandbox's `sandbox sql` / `sandbox python` lines for one job can be
joined on `job_id` and `run_id`:

```json
{"ts": "2026-11-04T18:20:07.201Z", "level": "info", "logger": "analyst_agent.sandbox", "msg": "sandbox sql", "job_id": "job_b11b687349494286", "run_id": "run_4f0c9a1d2e3b4c5d", "sandbox": "sql", "ok": true, "row_count": 1, "duration_ms": 3, "error": null}
```

`AGENT_LOG_JSON_ENABLED=true` switches the stderr handler to JSON lines. Otherwise text lines
carry the same fields as `[job_id=… run_id=…]`. `AGENT_LOG_LEVEL` sets the level. The API
configures logging at startup (`uvicorn analyst_agent.api.app:app`). The CLI and `eval run`
keep stderr at `WARNING` unless `--verbose` (`ask` only), `AGENT_LOG_LEVEL` or JSON logs are
set. The ids are context variables, so concurrent jobs never mix: each worker thread or asyncio
task has its own. `eval run`'s process-isolated task children (the default `--isolation process`) are configured
with the parent's format and level, so their loop and sandbox lines carry `task_id` and `run_id`
too.

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
