# Configuration

All runtime configuration is read from `AGENT_*` environment variables (or a `.env` file in the
working directory) by **one** pydantic-settings class,
[`analyst_agent.core.config.Settings`](../src/analyst_agent/core/config.py). The agent core, the
CLI, the API (`api/`), the eval harness (`eval/`) and the MCP server all use it; there are no
other settings classes. [`.env.example`](../.env.example) lists every variable with its default,
and `tests/test_config.py` fails if a `Settings` field is missing from this table or from
`.env.example`, or if a default here drifts from the code.

An empty value means *unset* (`None`) for optional settings: `AGENT_JOB_DB_PATH=` runs the API
memory-only, `AGENT_APPROVAL_ROW_THRESHOLD=` never gates on row counts, `AGENT_MODEL_L1=` falls
back to `AGENT_MODEL`.

## Feature flags

These five agent features are **off by default**, independent of each other, and can be combined. The observability flags (`AGENT_LANGFUSE_ENABLED`, `AGENT_TRACE_STORE_ENABLED`, `AGENT_LOG_JSON_ENABLED`) are also off by default; see [observability.md](observability.md).

| Flag | What it turns on | CLI override (`ask`) | Eval override (`eval run`) |
|---|---|---|---|
| `AGENT_ROUTING_ENABLED` | One cheap router call labels the question L1/L2/L3 and picks the working model; escalates on a second critic "revise" or a repeated tool error ([model-routing.md](model-routing.md)). | `--routing/--no-routing` | `--routing/--no-routing` |
| `AGENT_SEMANTIC_LAYER_ENABLED` | Retrieval (BM25 by default; `AGENT_SEMANTIC_BACKEND`) of governed metric definitions (`AGENT_SEMANTIC_METRICS_DIR`) into the first user message, plus extra planner rules. | `--semantic-layer/--no-semantic-layer` | `--semantic-layer/--no-semantic-layer` |
| `AGENT_STRUCTURED_REPORT_ENABLED` | `final_report` must use the structured schema (headline, key_numbers, method, caveats, charts); the Markdown is rendered from it, and a consistency check flags headline numbers missing from key_numbers and key numbers no tool result supports to the critic ([report-and-memory.md](report-and-memory.md)). | env only | env only |
| `AGENT_MEMORY_ENABLED` | Save verified findings per dataset fingerprint to SQLite (`AGENT_MEMORY_DB_PATH`) and inject the most relevant prior insights into the first user message as labelled prior context ([report-and-memory.md](report-and-memory.md)). | env only | env only |
| `AGENT_APPROVAL_ENABLED` | Human-in-the-loop gate before costly/risky tool calls (large `EXPLAIN` estimate, `run_python`, budget fraction). | `--require-approval/--no-require-approval`; `--auto-approve` approves without prompting | gates are **auto-approved by default**; `--no-auto-approve` leaves no approver (gated steps are denied) |

How each surface answers an approval request when `AGENT_APPROVAL_ENABLED=true`:

| Surface | Approver |
|---|---|
| CLI `ask` | y/n prompt on stderr; `--auto-approve` approves everything; no answer on stdin = denied |
| API / Streamlit UI | the job pauses (`approval_required` event, `pending_approval` on `GET /jobs/{id}`) until `POST /jobs/{id}/approve` or `AGENT_APPROVAL_TIMEOUT_S` (timeout = denied) |
| Eval harness | auto-approve (recorded as `decided_by: auto` in the trace); `--no-auto-approve` to measure the deny path |
| MCP server | not applicable: it exposes the tools, not the agent loop |

## All settings

| Variable | Default | Area | Description |
|---|---|---|---|
| `AGENT_MODEL` | `claude-opus-5-5` | Agent core | Working model for the agent loop (and every routed tier unless `AGENT_MODEL_L*` is set). |
| `AGENT_CRITIC_MODEL` | *(unset)* | Agent core | Model for the critic step; unset = `AGENT_MODEL`. |
| `AGENT_EFFORT` | `high` | Agent core | Reasoning effort: `low` / `medium` / `high` / `xhigh` / `max`. |
| `AGENT_MAX_TOKENS` | `16000` | Agent core | `max_tokens` per LLM call. |
| `AGENT_REFUSAL_FALLBACK` | `true` | Agent core | Send the server-side refusal-fallback beta (only to models that support it). |
| `AGENT_MAX_TURNS` | `15` | Agent core | Max agent turns per run (escalations grant one extra turn each). |
| `AGENT_TOKEN_BUDGET` | `400000` | Agent core | Token budget per run (input + output + cache). |
| `AGENT_MAX_CRITIC_ROUNDS` | `2` | Agent core | Max critic reviews per run. |
| `AGENT_MAX_RETRIES` | `4` | Agent core | Retries for transient API errors. |
| `AGENT_RETRY_BASE_DELAY` | `1.0` | Agent core | First retry delay, seconds (exponential backoff). |
| `AGENT_RETRY_MAX_DELAY` | `30.0` | Agent core | Max retry delay, seconds. |
| `AGENT_ARTIFACTS_DIR` | `artifacts` | Agent core | Where charts and other tool outputs are written; the API serves it under `/artifacts`. |
| `AGENT_MAX_TOOL_OUTPUT_CHARS` | `20000` | Agent core | Tool output is truncated to this many characters before it reaches the model. |
| `AGENT_ROUTING_ENABLED` | `false` | Model routing | **Feature flag.** Classify each question L1/L2/L3 and pick the working model per tier ([model-routing.md](model-routing.md)). |
| `AGENT_ROUTER_MODEL` | `claude-haiku-5-5` | Model routing | Classifier model for the router call. |
| `AGENT_ROUTER_EFFORT` | `low` | Model routing | Effort for the router call. |
| `AGENT_ROUTER_MAX_TOKENS` | `1024` | Model routing | `max_tokens` for the router call. |
| `AGENT_MODEL_L1` | *(unset)* | Model routing | Working model for L1 questions; unset = `AGENT_MODEL`. |
| `AGENT_MODEL_L2` | *(unset)* | Model routing | Working model for L2 questions; unset = `AGENT_MODEL`. |
| `AGENT_MODEL_L3` | *(unset)* | Model routing | Working model for L3 questions; unset = `AGENT_MODEL`. |
| `AGENT_ROUTER_FALLBACK_TIER` | `L3` | Model routing | Tier used when the router call fails or returns unparseable output. |
| `AGENT_SEMANTIC_LAYER_ENABLED` | `false` | Semantic layer | **Feature flag.** Retrieve governed metric definitions into the first user message. |
| `AGENT_SEMANTIC_METRICS_DIR` | *(unset)* | Semantic layer | Directory (or file) of `metrics.yaml` definitions. Unset means the definitions shipped with the installed package (`analyst_agent/semantic/metrics` in a wheel, `benchmarks/metrics` in a source checkout), whatever the working directory. An explicit relative path resolves against the working directory. |
| `AGENT_SEMANTIC_TOP_K` | `4` | Semantic layer | Metric definitions retrieved per question. |
| `AGENT_SEMANTIC_BACKEND` | `bm25` | Semantic layer | Scoring backend: `bm25`, `embedding` (sentence-transformers cosine) or `hybrid` (weighted mix). `embedding` / `hybrid` need the optional extra: `uv sync --extra embeddings`. |
| `AGENT_SEMANTIC_HYBRID_WEIGHT` | `0.5` | Semantic layer | `hybrid` only: embedding share of the score, 0–1 (0 = BM25 ranking, 1 = embedding ranking). Each backend is max-normalised per query first. |
| `AGENT_SEMANTIC_EMBEDDING_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | Semantic layer | sentence-transformers model; loaded lazily on first use (downloaded from Hugging Face if not cached). |
| `AGENT_SEMANTIC_EMBEDDING_CACHE_DIR` | `data/embeddings` | Semantic layer | On-disk cache of metric-definition vectors (one `.npz` per model, keyed by text hash), so a warm start does not load the model just to index. |
| `AGENT_STRUCTURED_REPORT_ENABLED` | `false` | Report | **Feature flag.** Require the structured `final_report` schema and run the headline / key-number consistency check before the critic. |
| `AGENT_MEMORY_ENABLED` | `false` | Memory | **Feature flag.** Store verified findings per dataset fingerprint and retrieve relevant prior insights into the planner's first message. |
| `AGENT_MEMORY_DB_PATH` | `data/memory.sqlite3` | Memory | SQLite file for the insight store (shared safely by the API job pool and eval workers). |
| `AGENT_MEMORY_TOP_K` | `3` | Memory | Prior insights retrieved per question (only from the same dataset fingerprint). |
| `AGENT_APPROVAL_ENABLED` | `false` | Human-in-the-loop | **Feature flag.** Gate costly or risky tool calls behind a human approval. |
| `AGENT_APPROVAL_ROW_THRESHOLD` | `1000000` | Human-in-the-loop | Gate `run_sql` when the `EXPLAIN` row estimate is above this (empty = never). |
| `AGENT_APPROVAL_PYTHON` | `true` | Human-in-the-loop | Gate every `run_python` call. |
| `AGENT_APPROVAL_BUDGET_FRACTION` | `0.8` | Human-in-the-loop | Gate the next step once spend exceeds this fraction of `AGENT_TOKEN_BUDGET` (empty = never). |
| `AGENT_APPROVAL_TIMEOUT_S` | `300.0` | Human-in-the-loop | API only: seconds to wait for `POST /jobs/{id}/approve`; no decision = denied. |
| `AGENT_DATA_DIR` | `data` | API | Service state; uploads live in `<data_dir>/uploads`. |
| `AGENT_JOB_DB_PATH` | `data/jobs.sqlite3` | API | SQLite write-through job store; empty = in-memory only. |
| `AGENT_JOB_MEMORY_LIMIT` | `500` | API | Finished jobs kept in memory (older ones are served from SQLite). |
| `AGENT_MAX_CONCURRENT_JOBS` | `4` | API | Worker threads running agent jobs. |
| `AGENT_MAX_UPLOAD_MB` | `50` | API | Max dataset upload size. |
| `AGENT_CORS_ORIGINS` | `[]` | API | Allowed CORS origins, as a JSON list. |
| `AGENT_LANGFUSE_ENABLED` | `false` | Observability | **Feature flag.** Send each run's span tree (API jobs, CLI `ask`, eval tasks) to Langfuse and push eval scores onto the traces (keys from `LANGFUSE_*`, see [observability.md](observability.md)). |
| `AGENT_TRACE_STORE_ENABLED` | `false` | Observability | **Feature flag.** Save each run's span tree as `<AGENT_TRACES_DIR>/<id>.json` (eval: `<run_dir>/traces/`) for `analyst-agent trace show`. |
| `AGENT_TRACES_DIR` | `traces` | Observability | Where saved traces go (CLI and API). |
| `AGENT_LOG_JSON_ENABLED` | `false` | Observability | **Feature flag.** One JSON object per log line on stderr, with `run_id` / `job_id` / `task_id` correlation fields. Text logs carry the same fields otherwise. |
| `AGENT_LOG_LEVEL` | `INFO` | Observability | Root log level for the CLI, API and eval harness. |
| `AGENT_JUDGE_MODEL` | *(unset)* | Eval | LLM judge model for L3/ADV rubric tasks; unset = `AGENT_MODEL`. |
| `AGENT_JUDGE_EFFORT` | `low` | Eval | Effort for the judge. |
| `AGENT_EVAL_CONCURRENCY` | `4` | Eval | Default `eval run --concurrency`. |
| `AGENT_EVAL_TIMEOUT_S` | `300.0` | Eval | Default `eval run --timeout` per task, seconds. |
| `AGENT_EVAL_RUNS_DIR` | `runs` | Eval | Default `eval run --runs-dir`. |

## Other environment variables

These are not `Settings` fields:

| Variable | Read by | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic SDK | API key (not needed for `--dry-run`, `--agent dry-run` or `--agent oracle`). |
| `ANTHROPIC_BASE_URL` | Anthropic SDK | Optional API base URL (CI points it at a dead port to prove no calls are made). |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` | Langfuse SDK | Used when `AGENT_LANGFUSE_ENABLED=true` ([observability.md](observability.md)). |
| `AGENT_API_URL` | `frontend/app.py` | Base URL of the API (default `http://localhost:8000`; `http://api:8000` in compose). |
| `AGENT_API_ID_TOKEN` | `frontend/app.py` | `true` to call a private Cloud Run API with a Google ID token ([deploy/cloudrun.md](../deploy/cloudrun.md)). |
| `PORT` | container `CMD` | Listen port (API 8000, UI 8501; Cloud Run injects 8080). |

The two `AGENT_API_*` variables belong to the Streamlit UI, which runs in its own image without
the agent package (only `streamlit` + `httpx`), so they are read with `os.environ` there
rather than through `Settings`.
