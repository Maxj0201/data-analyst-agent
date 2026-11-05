# Changelog

All notable changes to this project. Versions follow [SemVer](https://semver.org/); dates are UTC.

## [Unreleased]

### Added
- `LICENSE` (MIT), `SECURITY.md` (reporting, sandbox guarantees and limits) and a pull request
  template that mirrors `CLAUDE.md`.
- `.pre-commit-config.yaml` (`make hooks`): ruff, ruff format, `uv lock --check`, and
  `scripts/check_prompt_versions.py`, which fails when a prompt's text changes without a version
  bump.
- `scripts/check_docs.py`, run by `make lint` (and so by CI): every relative link and anchor in
  the Markdown resolves, every path in backticks exists, every `AGENT_*` name is a `Settings`
  field, every documented CLI command and option parses, and no `TODO`, `TBD`, `placeholder` or
  double-brace template text is left outside `docs/resume.md` and `docs/eval-runbook.md`.

### Changed
- CI now runs `make lint`, `make coverage` and `make eval`; `make check` runs `install` first, so
  it is exactly what CI runs.
- The "Landlock unavailable" warning is logged once per process by the parent
  (`analyst_agent.sandbox.python_runner`, `WARNING`), not only written to the child's stderr.
- `docs/kickoff/` moved to `docs/history/kickoff/` (historical).

### Removed
- The committed `traces/` run outputs; `traces/`, `*.sqlite3` and images outside `docs/assets/`
  are now git-ignored. The duplicate `httpx` dev dependency.

## [0.1.0] - 2026-11-05

The first release. Pull requests #1–#27 were integrated in six rounds; each round is
written up in [docs/integration-notes.md](docs/integration-notes.md). Every new capability ships
behind an `AGENT_*_ENABLED` flag that defaults to off. With every flag off, the agent behaves as
the original plan → execute → critic loop.

**Not measured yet:** no part of this release has been run against a live model. Every check
below is offline, with a scripted LLM or a fake API. The README *Results* table is still empty;
[docs/eval-runbook.md](docs/eval-runbook.md) describes the paid run that will fill it.

### Agent core (`core/`)

- A hand-written Anthropic Messages API loop (`core/loop.py`, one `AgentState`):
  - the planner writes a `<plan>`, then calls `inspect_schema`, `run_sql`, `run_python` and
    `make_chart`, and finishes with `final_report`.
  - limits: `AGENT_MAX_TURNS`, `AGENT_TOKEN_BUDGET` and `AGENT_MAX_CRITIC_ROUNDS`.
  - two `final_report` calls in one response are handled (review M5).
- Every model call goes through one wrapper, `core/llm.py`. It handles:
  - prompt caching and per-model, per-role token and cost accounting
  - retries with backoff
  - the refusal-fallback beta, sent only to models that support it
  - Message Batches (`create_batch`), used by the eval judge
- **Critic.** A separate prompt sees only the answer, the SQL/code that ran and the result
  tables. It returns `pass | revise` for up to two rounds; `0` turns it off. It fails open to
  `completed_with_issues`.
- **Structured reports** (`AGENT_STRUCTURED_REPORT_ENABLED`). The report has a headline, key
  numbers, a table, charts and caveats. A deterministic consistency check runs before the
  critic.
- **Insight memory** (`AGENT_MEMORY_ENABLED`). Past headlines and key numbers are retrieved by a
  dataset fingerprint. The fingerprint hashes every column, and it fails closed when the
  checksum is unavailable (review-2 M1).
- **Prompt versioning.** Each prompt is a `Prompt(name, version, text)` with a sha256
  fingerprint. Versions are recorded in every trace, eval `meta.json` and API job result.
  `tests/test_prompts.py` fails when a prompt's text changes without a version bump.
  - Release versions: planner **1.2.0**, critic 1.1.0, all others 1.0.0.
  - Planner 1.2.0 puts every figure the report cites into `key_numbers`. The v2 rubric judge
    gives no credit for a figure that appears only in the prose.
- **`ask --dry-run`** swaps in a scripted LLM (`core/fake_llm.py`). The loop, tools, sandbox and
  critic are real. It works with every flag.

### Sandbox (`sandbox/`)

- **SQL** runs in an in-memory, read-only DuckDB:
  - one statement per call, from an allowlist: `SELECT`, `WITH` or `EXPLAIN`
  - external access off and the configuration locked
  - a 30 s timeout and a 200-row cap
- **Python** runs in a new subprocess for every call. It gets rlimits, a scrubbed environment
  and a 60 s process-group kill, plus:
  - a **seccomp-bpf** filter: no `execve`, `fork`, sockets, mounts or metadata changes
  - a **Landlock** ruleset: it can read only the libraries and write only its temp dir
  - `PR_SET_PDEATHSIG`: the child dies with its parent (review-2 M3)
- Inputs: CSV, Parquet, SQLite and DuckDB files.
- Callers that run jobs concurrently create one sandbox per job (`tools.create_sandbox`,
  `ToolContext.sandbox`). The CLI uses the module-level sandbox.
- **Tests:** a grammar-based escape fuzzer (500 snippets) runs nightly. It found eight classes
  of gap, all now closed (`AF_UNIX` connects, `chmod`/`utimes`, …). There are 265 sandbox tests.
  See [docs/sandbox.md](docs/sandbox.md).

### Eval (`eval/`, `benchmarks/`)

- **The 80-task benchmark** (v2) runs over seeded synthetic data whose ground truth comes from
  the generator: 24 L1, 24 L2, 16 L3 (graded against a rubric) and 16 ADV (adversarial).
- **Runner:**
  - each task runs in its own process, and a timeout kills the whole process group
  - `--budget-usd` counts the spend of tasks that timed out or crashed (review-2 M2)
  - `--resume` carries over all recorded spend
  - `--batch` sends the judge calls as one Message Batch; a cancelled run sends none
- **Reports:** `report` and `compare` give accuracy by tier and tag, cost, p50/p95 latency, the
  critic correction rate, routing, and the prompt and judge versions.
- **Judge 2.0.0** (#26), stricter than 1.0.0:
  - it must quote the report sentence that satisfies each rubric point
  - every figure in that sentence must also be in `key_numbers`
  - the scorer re-checks the quotes itself
- **Judge calibration** (#26):
  - `benchmarks/judge_calibration/` holds 24 hand-labelled reports (8 L3 tasks × correct /
    subtly wrong / off-topic)
  - `eval judge-calibrate` reports verdict and point agreement, and Cohen's kappa
  - `--judge-samples N` measures the judge's variance
- **Experiments:** `scripts/run_experiments.sh` runs eight arms under budget caps.
  `scripts/preflight.sh` checks keys, models, extras and the data, and estimates the cost.
- **CI** makes no API calls. It runs:
  - lint and the format check
  - the tests under a branch-coverage gate
  - the oracle agent, which must score 100%
  - a dry run of the real loop, then the same dry run with every flag on

### Semantic layer (`semantic/`)

- 22 governed metric definitions (`benchmarks/metrics/*.yaml`), bundled in the wheel as
  `analyst_agent/semantic/metrics`.
- Three retrieval backends: BM25, embedding and hybrid. The embedding and hybrid backends need
  the `embeddings` extra. The layer is behind `AGENT_SEMANTIC_LAYER_ENABLED`.
- **Analyst vocabulary** (`vocabulary.yaml`, #25). This phrase table expands BM25 queries. With
  it and the synonym fixes, BM25 scores top-1 / top-3 / MRR **1.000 / 1.000 / 1.000** on the 33
  benchmark cases (before: 0.848 / 0.909 / 0.896) and 0.600 / 0.900 / 0.758 on the 10 held-out
  paraphrases. The CI guard is back at top-1 ≥ 0.9.
- `python -m analyst_agent.semantic eval` runs the retrieval benchmark, with
  `--no-vocabulary` for ablation.

### Model routing (`core/router.py`)

- A cheap router call labels each question L1, L2 or L3 and picks the working model
  (`AGENT_ROUTING_ENABLED`).
- The run escalates one tier after a second critic `revise`, or after the same tool fails twice
  in a row. Human denials never escalate.
- Cost is tracked per model and per role. See [docs/model-routing.md](docs/model-routing.md).

### Human in the loop (`core/approval.py`)

- An approval gate runs before risky steps (`AGENT_APPROVAL_ENABLED`):
  - a `run_sql` whose DuckDB `EXPLAIN` estimate is large, including after `EXPLAIN ANALYZE`
    or a leading comment (review M6)
  - any `run_python`
  - spend past a budget fraction
- Each surface answers it differently:
  - the CLI prompts y/n
  - the API/UI pause on `POST /jobs/{id}/approve`, where a timeout counts as a deny
  - eval auto-approves
- A denial returns to the model as a tool error, so it re-plans.

### Observability (`core/tracing.py`, `core/logs.py`)

- Each run becomes a span tree: run → turn → llm / tool / critic / approval / route. Every node
  carries cost, model, tier and prompt version.
- The tree goes to Langfuse (`AGENT_LANGFUSE_ENABLED`), where eval scores attach to each task's
  trace, or to a local store (`AGENT_TRACE_STORE_ENABLED`, `analyst-agent trace ls|show`).
- JSON logs (`AGENT_LOG_JSON_ENABLED`) carry `run_id`, `job_id` and `task_id` on every line,
  including from process-isolated eval tasks (review-2 L1).

### API, UI and deployment (`api/`, `frontend/`, `Dockerfile`)

- **FastAPI service:**
  - dataset upload, with the size limit enforced while streaming (review M3)
  - background jobs and a SQLite job store; failures always end the job (review M8)
  - SSE events, resumable with `Last-Event-ID`
  - approvals, Markdown reports and artifacts
  - bundled sample datasets (`AGENT_SAMPLES_ENABLED`)
- **Streamlit UI:** a step timeline, approve/deny with a note, report tiles, cost and tokens,
  and the raw trace. Untrusted Markdown cannot render images on any surface, which
  `tests/frontend/test_no_untrusted_images.py` checks (review M2, review-2 M4).
- **MCP server** (`analyst-agent-mcp`): the four sandboxed tools over stdio, with one sandbox
  per session. See [docs/mcp.md](docs/mcp.md).
- **Docker** (#27):
  - two targets, `api` and `frontend`
  - the embeddings image uses CPU-only torch
  - the trace store lives on the data volume
  - compose forwards every flag
- **Docker CI and probes** (#27):
  - `.github/workflows/docker.yml` builds both images and runs the H1/H2 sandbox probes and
    the UI e2e inside them
  - `scripts/sandbox-probes/` checks seccomp and Landlock in a container
  - See [docs/deploy.md](docs/deploy.md); Cloud Run deploys as gen2.
- **UI e2e** (`scripts/ui-e2e/`): headless Chromium drives the real app with every flag on.
  24/24 checks pass, locally and against the compose stack.
- **Demo assets** (#24): screenshots, a trace example and a 3-minute demo script
  ([docs/assets/](docs/assets/)).

### Security review rounds

- **Review 1** ([docs/review.md](docs/review.md)), 2 High and 8 Medium findings, all fixed with
  regression tests:
  - **H1:** a shell through `_posixsubprocess.fork_exec`
  - **H2:** file I/O through pyarrow's native readers. Seccomp and Landlock now close both.
  - **M1:** a case-sensitive untrusted-data fence
  - **M2:** zero-click image exfiltration in the UI
  - **M3:** the upload limit was checked only after spooling
  - **M4:** pyplot global state and unbounded `bins`
  - **M5:** two `final_report` calls in one response
  - **M6:** an approval-gate bypass
  - **M7:** `preexec_fn` in a threaded process
  - **M8:** wedged jobs
- **Review 2** ([docs/review-2.md](docs/review-2.md)), no High and 4 Medium findings, all fixed
  with regression tests:
  - **M1:** stale memory fingerprints
  - **M2:** budget blind to timed-out tasks
  - **M3:** sandbox children outliving killed tasks
  - **M4:** images in `st.metric` values
- **Low findings** from both reviews are documented, each with the reason it was left. The
  review-2 docs audit was also completed.

### Measured offline at the tag

| Check | Result |
|---|---|
| Tests | 1001 passed, 0 skipped, 1 slow deselected |
| Branch coverage | 94.2% overall · `sandbox/` 99.4% · `core/loop.py` 100% |
| Oracle, all 80 tasks | 100% on the 64 gradable tasks |
| Sandbox escape fuzzer | 500/500 blocked or killed (fixed seed and a new seed) |
| UI e2e | 24/24, locally and against the compose stack |
| Docker images (`api` / `frontend`) | build; 984 / 832 MB on disk, 222 / 191 MB compressed; sandbox probes pass inside |
| `eval judge-calibrate --fake` | 100% agreement, κ 1.000 (oracle fake; this checks the plumbing) |

[0.1.0]: https://github.com/maxj0201/data-analyst-agent/releases/tag/v0.1.0
