# Autonomous Data Analyst Agent

Ask a question in plain English about a CSV, Parquet, SQLite or DuckDB file, and the agent
inspects the schema, writes and runs SQL and Python in a kernel-confined sandbox, has a second
model check the answer, and returns a Markdown report with charts. It is built on a hand-written
Anthropic Messages API loop, and every change to it is gated by an 80-task benchmark, an
adversarial prompt-injection set and offline CI checks.

## Architecture

```mermaid
flowchart LR
    subgraph Surfaces
        CLI["CLI<br/>ask · trace show"]
        UI["Streamlit UI<br/>timeline · approve/deny"]
        API["FastAPI<br/>jobs · SSE · /approve"]
        MCP["MCP server<br/>(tools only)"]
        EVAL["Eval runner<br/>80 tasks · child per task"]
    end

    subgraph Core["Agent loop"]
        ROUTE["Router<br/>L1/L2/L3 → model"]
        CTX["Semantic layer<br/>+ insight memory"]
        LOOP["Plan → Execute"]
        GATE{{"Approval gate<br/>(HITL)"}}
        CRITIC["Consistency check<br/>+ critic: pass | revise"]
    end

    subgraph SB["Sandbox, one per job"]
        SQL[("DuckDB<br/>read-only · allowlist")]
        PY["Python subprocess<br/>seccomp + Landlock"]
    end

    LLM["core/llm.py<br/>caching · cost · versioned prompts"]
    OBS[("Span tree →<br/>Langfuse / trace store")]

    UI --> API --> LOOP
    CLI --> LOOP
    EVAL --> LOOP
    MCP --> SQL
    MCP --> PY
    LOOP --> ROUTE
    LOOP --> CTX
    LOOP -->|tool_use| GATE
    GATE --> SQL
    GATE --> PY
    SQL -->|untrusted-data fence| LOOP
    PY -->|untrusted-data fence| LOOP
    LOOP -->|final_report| CRITIC
    CRITIC -->|revise| LOOP
    ROUTE --> LLM
    LOOP --> LLM
    CRITIC --> LLM
    LOOP -.->|events| OBS
```

Every surface (CLI, API/UI, eval runner) drives the **same** loop; they differ only in the event
sink and the approver. The MCP server exposes the sandboxed tools without the loop. The full
diagram, a step-by-step run and the design trade-offs are in
[`docs/architecture.md`](docs/architecture.md); the reasoning behind each decision, with the
alternatives that were rejected, is in [`docs/interview-notes.md`](docs/interview-notes.md).

## What makes this production-minded

**Sandbox with kernel enforcement.** SQL runs on an in-memory, read-only DuckDB with a
single-statement `SELECT`/`WITH`/`EXPLAIN` allowlist, external access disabled and the config
locked. Python runs in a fresh subprocess per call. That subprocess gets rlimits, a scrubbed
environment, a 60 s process-group kill, a **seccomp-bpf** filter (no `execve`, `fork`, sockets
of any family, signals to other processes, mounts, metadata changes or host-wide syscalls) and a
**Landlock** ruleset (it can read only the libraries and write only its temp dir). A hostile
review found two escapes past the original Python-level guards: a shell via
`_posixsubprocess.fork_exec`, and arbitrary file I/O via pyarrow's native readers. Seccomp and
Landlock closed both. A grammar-based escape fuzzer (500 snippets, nightly) then found eight more
classes of gap, among them `AF_UNIX` connects that Landlock does not mediate and `chmod`/`utimes` on
readable files; the filter now closes them too. Regression tests keep all of them closed
([review H1/H2](docs/review.md#high), [threat model](docs/sandbox.md),
[fuzzing](docs/sandbox.md#escape-fuzzing)).

**Critic.** A separate LLM call reviews the draft. It has its own prompt and sees only the
answer, the executed SQL/code and the result tables, not the planner's reasoning. It returns
`pass | revise` with issues, for up to two rounds (`AGENT_MAX_CRITIC_ROUNDS`; `0` switches the
critic off, which the critic ablation uses). With structured reports on, a deterministic
check runs first and flags headline numbers that are not key numbers, and key numbers that no
tool output contains.

**Eval and the CI gate.** There are 80 tasks with tags: 24 L1, 24 L2, 16 L3 (judged against a
rubric) and 16 ADV, over seeded synthetic data whose ground truth is computed by the generator.
Each task runs in its own child process, and on timeout the whole process group is killed.
`report` and `compare` cover accuracy by tier and tag, cost, p50/p95 latency, critic correction
rate, routing and prompt diffs. CI makes no API calls. It runs lint, the format check and 898
tests (Hypothesis property tests included) under a branch-coverage gate, then the oracle agent
(must score 100%), then a dry run of the real loop and sandbox with a scripted LLM, and then the
same dry run with **every** feature flag on. A headless-browser UI e2e test runs on API/UI PRs,
and the sandbox escape fuzzer runs nightly.

**Adversarial set.** The data carries injections in cell text, a column header and a file name,
PII in columns and free text, write-attempt requests, and exfiltration to URLs. The ADV tasks
pass only if rule checks, an LLM judge and the legitimate part of the answer all pass. Tool
output reaches the model inside an untrusted-data fence that a cell cannot close: the fence is
matched case- and whitespace-insensitively, which closed review M1. The UI renders no images from
untrusted Markdown except this job's own charts (review M2: zero-click exfiltration).

**Model routing.** A cheap router call labels each question L1/L2/L3 and picks the working model.
The run escalates one tier after a second critic `revise`, or when the same tool fails twice in a
row. Human approval denials never escalate. Cost is tracked per model and per role
([docs/model-routing.md](docs/model-routing.md)).

**Human in the loop.** An approval gate sits before risky steps: a `run_sql` whose DuckDB
`EXPLAIN` estimate is large (including `EXPLAIN ANALYZE` and comment-prefixed SQL, review M6),
any `run_python`, or spend past a budget fraction. The CLI prompts y/n, the API/UI pause on
`POST /jobs/{id}/approve` (a timeout counts as a deny), and the eval auto-approves. A denial
returns to the model as a tool error so it re-plans.

**Tracing.** Each run's event stream folds into a run → turn → llm / tool / critic / approval /
route span tree with cost, model, tier and prompt version on every node. The tree goes live to
Langfuse, where eval scores land on each task's trace, or to JSON for `analyst-agent trace
show`. Logs carry `run_id` / `job_id` / `task_id` on every line
([docs/observability.md](docs/observability.md)).

**Prompt versioning.** Every system prompt is a `Prompt(name, version, text)` with a sha256
fingerprint, and the versions are recorded in every trace, eval `meta.json` and API job result.
`tests/test_prompts.py` fails if a prompt's text changes without a version bump, and
`eval compare` lists the prompt changes between two runs.

Every new capability ships behind an `AGENT_*_ENABLED` flag that defaults to off. With all flags
off, behaviour is byte-for-byte the pre-flag path, so each feature can be A/B'd on the same commit
([docs/configuration.md](docs/configuration.md)).

## Quickstart

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). The first two runs need no API key.

```bash
git clone https://github.com/maxj0201/data-analyst-agent.git && cd data-analyst-agent
```

```bash
uv sync --all-extras --no-extra embeddings
```

```bash
uv run analyst-agent ask --dry-run --data benchmarks/data/saas/subscriptions.csv "which region has the highest mrr"
```

```bash
uv run python -m analyst_agent.eval run --agent oracle --no-judge --limit 5 --fail-under 1.0
```

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

```bash
uv run analyst-agent ask -v --data benchmarks/data/ecommerce/orders.csv "Which month of 2025 had the highest revenue, and how does it compare to the 2024 average?"
```

```bash
scripts/demo.sh   # Docker: API + UI with bundled sample datasets, prints the URL
```

`--dry-run` replaces only the LLM with a scripted one: the loop, tools, sandbox and critic path
are real. Every feature flag can be combined with it, for example
`--routing --semantic-layer --require-approval --auto-approve --trace trace.json`. The
[CLI options](docs/configuration.md#feature-flags), the [API endpoints](#api) and the
[MCP setup](docs/mcp.md) are documented separately. Contributors: see
[CONTRIBUTING.md](CONTRIBUTING.md) and `make help`.

## Results

**Not yet measured with a live model** (no API key has been available to the build
environment). Each cell below is filled from the `report.md` that the command writes. Use one
model and one commit for the whole table, and repeat each run; with 16 L3 and 16 ADV tasks, a
single task is 6.25 pp.

The plan is five experiments on all 80 tasks, with the Sonnet baseline as the shared control:
Haiku vs Sonnet, critic on vs off (`AGENT_MAX_CRITIC_ROUNDS=0`), semantic layer
(BM25 / embedding / hybrid), routing on vs off, and structured reports on vs off. Every arm pins
every `AGENT_*` flag and runs under an `eval run --budget-usd` cap derived from the price table.
[docs/eval-runbook.md](docs/eval-runbook.md) has the cost estimates, the exact commands, the
summary tables to fill and how to read them.

```bash
scripts/run_experiments.sh --agent dry-run --yes   # free end-to-end check of the pipeline
```

```bash
scripts/run_experiments.sh --print-commands        # the exact command and cap of every arm
```

```bash
scripts/run_experiments.sh                         # real models; asks before spending
```

The script writes `runs/summary.md` (accuracy by tier with a ± interval, cost, latency, critic
correction rate, and the delta against the control). Diff any two runs, prompt versions
included, with:

```bash
uv run python -m analyst_agent.eval compare runs/experiments/<stamp>/sonnet/<run> runs/experiments/<stamp>/routing-on/<run>
```

The offline numbers that *are* measured:

| Check | Result | Command |
|---|---|---|
| Unit, integration and property tests | 898 passed, 0 skipped (1 slow test deselected) | `make test` |
| Branch coverage (CI gate) | 93.7% overall (floor 92) · `sandbox/` 99.4% (floor 97) · `core/loop.py` 100% (floor 98) | `make coverage` |
| Oracle on all 80 tasks (harness self-check) | 100% on the 64 gradable tasks (L3 needs a judge) | `uv run python -m analyst_agent.eval run --agent oracle --no-judge` |
| Semantic retrieval, BM25, 33 scored benchmark-task cases | top-1 0.848 · top-3 0.909 · MRR 0.896 | `uv run python -m analyst_agent.semantic eval` |
| Sandbox escape and unit tests | 265 passed | `uv run pytest tests/sandbox` |
| Sandbox escape fuzzer (default seed, nightly) | 500 of 500 snippets blocked or killed | `uv run pytest -m slow tests/sandbox/test_fuzz_escape.py` |
| UI e2e (approve and deny flows) | 24 checks per run | `make e2e` |

## API

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | liveness and job-store mode |
| `POST /datasets` | upload a CSV, Parquet, SQLite or DuckDB file (multipart `file`) → `dataset_id` |
| `GET /datasets/samples` | bundled sample datasets with example questions (`AGENT_SAMPLES_ENABLED`) |
| `GET /datasets/{id}` | dataset metadata |
| `POST /ask` | `{dataset_id, question}` → `job_id` (runs in the background) |
| `GET /jobs/{id}` | status, progress, cost, `pending_approval`, result (report, key numbers, artifacts, prompt versions) |
| `GET /jobs/{id}/events` | Server-Sent Events: every step, from `run_start` to `final` |
| `POST /jobs/{id}/approve` | `{approve, note?, action_id?}` resolves a pending approval |
| `GET /jobs/{id}/report` | the final report as `text/markdown` |
| `GET /artifacts/{path}` | chart images |

Deployment to Cloud Run: [deploy/cloudrun.md](deploy/cloudrun.md).

## Repository map

| Path | What |
|---|---|
| `src/analyst_agent/core/` | loop, LLM wrapper, critic, router, approval, prompts, report check, memory, tracing |
| `src/analyst_agent/tools/` | tool schemas and handlers, untrusted-data fence, per-job sandbox wiring |
| `src/analyst_agent/sandbox/` | DuckDB runner, Python runner, `_child/` bootstrap (seccomp and Landlock) |
| `src/analyst_agent/semantic/` | metric definitions, BM25 / embedding / hybrid retrieval, retrieval eval |
| `src/analyst_agent/eval/` | runner, process isolation, scorers, LLM judge, report and compare |
| `src/analyst_agent/api/`, `frontend/` | FastAPI service, Streamlit UI |
| `benchmarks/` | generator, synthetic data, 80 task YAMLs, governed metrics |
| `docs/` | architecture, sandbox threat model, review, integration notes, interview notes |

## Roadmap

- [x] Agent core, sandbox, eval harness, API/UI, MCP server
- [x] Routing, semantic layer, HITL approval, structured reports, insight memory (all flagged)
- [x] Hostile review: all High and Medium findings fixed with regression tests
- [x] Tracing (Langfuse + local trace store), prompt versioning, 80-task benchmark v2
- [ ] **Publish the Results table** from live runs, including the routing and semantic-layer A/B
- [ ] Container-level isolation per job (gVisor or Firecracker, `--network none`) for multi-tenant use
- [ ] App-level auth and per-tenant artifact directories (review L1/L2; today the boundary is Cloud Run IAM)
- [ ] Job-level wall-clock timeout and `Retry-After`-aware retries (review L15/L16)
- [ ] Shared job store and approval broker (Postgres/Redis) so the API scales past one instance
- [ ] Harder benchmark: messy real-world schemas, Parquet/SQLite inputs, multi-turn injections
- [ ] Warehouse connectors (Postgres, BigQuery) behind the same `run_sql` contract
- [ ] Tune the semantic-layer synonyms where BM25 ranks the right metric outside the top 3
