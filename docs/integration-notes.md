# Integration notes

## Round 2: #6, #10, #8, #9, #7, #11 (2026-11-04)

Merged into `main` in this order. After each merge I ran `uv sync --all-extras`,
`uv lock --check`, `uv run ruff check`, `uv run ruff format --check` and `uv run pytest`, and got
everything green before the next merge. Final suite: **520 passed, 0 skipped**.

| Order | PR | Branch | Result | Tests after |
|---|---|---|---|---|
| 1 | #6 | `chore/consistency` | clean merge | 323 |
| 2 | #10 | `feat/hardening` | clean merge | 334 |
| 3 | #8 | `feat/semantic-layer` | 2 conflicts (`core/prompts.py`, `core/loop.py`) | 401 |
| 4 | #9 | `feat/model-routing` | 8 conflicts, plus picklability fix | 428 |
| 5 | #7 | `feat/mcp-server` | clean merge (`pyproject.toml` and `uv.lock` auto-merged) | 444 |
| 6 | #11 | `feat/hitl` | 4 conflicts, plus escalation fix | 493 |
| – | | integration commits (below) | | 520 |

### Conflicts resolved

**#8 semantic layer (onto #10 hardening)**
- `core/prompts.py`:
  - The docstring keeps both the versioning rules (#10) and the "per-question content goes in
    the first user message" note (#8).
  - The semantic-layer section (`SEMANTIC_LAYER_RULES`, `planner_system`,
    `render_metric_reference`) comes before the `Prompt` registry.
  - `SEMANTIC_LAYER_RULES` gets its own versioned prompt, `semantic_layer_rules@1.0.0`, because
    it changes the planner's system prompt when the flag is on.
  - `tests/test_prompts.py` pins it and checks that `planner_system(True)` is the planner prompt
    plus those rules.
- `core/loop.py` imports: union (`planner_system`, `prompt_versions`, `render_metric_reference`).

**#9 model routing**
- `core/prompts.py`: `ROUTER_SYSTEM` added next to the semantic section and registered as
  `router@1.0.0` in `PROMPTS`. It is pinned in `tests/test_prompts.py`.
- `core/loop.py`, five hunks, all taken as the union of both sides:
  - imports
  - `AgentState` fields (semantic and routing)
  - `to_dict` (`semantic_layer`, `prompts`, `routing`)
  - `AgentLoop.__init__` (`retriever=`, `router=`)
  - `_run`: the router runs first, then semantic retrieval goes into the first message, and the
    planner system prompt is `planner_system(flag)`. Each turn passes `model=state.model`.
- `eval/agent.py`:
  - The trace order is now documented: `route`, `semantic_retrieval`, plans, tool calls,
    escalations, approvals, critic.
  - `_run_core` takes both #10's `llm_factory` (dry run) and #9's `routing_enabled`.
  - **`make_core_agent` returned a closure, which #10's process isolation rejects as
    unpicklable.** So `eval run --routing` would have failed in the default `--isolation
    process`. It now returns a frozen dataclass, `CoreAgent(dry_run, routing_enabled,
    auto_approve)`. `--agent dry-run` also honours `--routing`.
  - Covered by `test_dry_run_with_routing_forced_on_runs_in_child_processes`.
- `eval/cli.py`: both option sets (`--isolation`, `--fail-under`, `--fail-on-error`, `--routing`).
- `eval/report.py`: both `semantic_retrieval()` and the routing summary. The routing groups'
  accuracy now leaves out skipped (ungradable) tasks, the same rule #10 introduced for the headline
  accuracy.
- `tests/eval/test_report.py`: both sides' tests.
- `core/fake_llm.py`: no textual conflict. `dry_run_llm` now also scripts a router verdict (L2 for
  a grouped query, L1 otherwise), so dry runs with routing on are routed rather than falling back.
  #10 and #11 did not touch this file, so the router queue, per-call model recording, `effort` /
  `max_tokens` kwargs and dry-run script all coexist.
- `.env.example`, `README.md`: union, later rewritten (see Docs).

**#7 MCP server**: no conflicts. `pyproject.toml` has every dependency and extra: `scipy` and
`rank-bm25` in `dependencies`; extras `api`, `frontend` and `mcp`; and the `dev` group.
`uv lock` was a no-op and `uv lock --check` passes.

**#11 human-in-the-loop**
- `api/agent.py`: #11's `core_runner(..., approver=None)` signature, with #6's "uploaded dataset"
  wording (uploads are no longer CSV-only).
- `core/config.py`: semantic and approval settings, both kept (consolidated later; see Config).
- `core/loop.py`:
  - imports: routing, semantic, approval and `ToolResult`
  - `to_dict`: adds `approvals`
  - `__init__`: `retriever`, `router`, `approver`, `policy`
  - tool dispatch: `_gate()` → execute → a record tagged with `state.model` → `turn_records` for
    escalation
- **Fix: approval denials no longer count as tool errors for escalation.** Two denied `run_sql`
  calls in a row used to escalate to a stronger model, which was a human decision being treated
  as a model failure. Records carrying `data["approval"]` are now skipped. Covered by
  `test_human_denials_do_not_escalate`, which fails without the fix.
- `tests/test_cli.py`: both sides' tests.

### Integration work (not textual conflicts)

- **One settings class.** `ApiSettings` (`api/settings.py`) and `EvalSettings`
  (`eval/config.py`) are folded into `core.config.Settings` and the two files are deleted.
  - `artifacts_dir` was defined twice.
  - The sandbox's fallback artifacts dir read `AGENT_ARTIFACTS_DIR` from `os.environ`, so `.env`
    was ignored there. It now reads `Settings` too.
  - Empty values now mean unset (`env_parse_none_str=""`). Before, `AGENT_JOB_DB_PATH=` (which
    the docs called memory-only mode) and `AGENT_APPROVAL_ROW_THRESHOLD=` failed validation or
    gave `Path(".")`.
  - The new `docs/configuration.md` has one table of all 41 settings with their defaults, plus
    the feature-flag matrix. `.env.example` is regenerated from the class.
  - `tests/test_config.py` fails if a field is missing from either file, if a documented default
    drifts, or if another `BaseSettings` class appears.
- **Eval auto-approves gated steps.** `RunConfig.auto_approve` (default true) and
  `eval run --auto-approve/--no-auto-approve`. The core and dry-run agents get an `AutoApprover`;
  with `--no-auto-approve` there is no approver, so gates deny.
  - Approval decisions are added to the eval trace (`{"type": "approval", ...}`).
- **Bug fix (#10): forkserver children used a stale environment.** They inherited the
  environment from when the fork server started, not the parent's current one, so `AGENT_*`
  changes made after the first task (a test's monkeypatch, a second run in one process) were
  ignored. `run_in_subprocess` now passes `os.environ` to the child, which applies it and clears
  `get_settings`' cache.
  - Found by `test_eval_auto_approves_gated_steps_by_default`: it passed alone and failed in the
    suite.
- **API job results carry prompt versions.** `AgentOutcome.prompts` and `JobResult.prompts` hold
  them, and so does the `final` SSE event. They come from `AgentResult.to_dict()["prompts"]`, the
  same data as the trace.
- **CLI:** `ask --routing/--no-routing` and `--semantic-layer/--no-semantic-layer` are added next
  to `--require-approval` and `--auto-approve`. `tests/test_cli.py` runs `ask --dry-run` over all
  16 combinations of routing × semantic × {auto, prompt yes, prompt no, off}.
- **API/UI events:** `route`, `escalation` and `semantic_retrieval` already reached the SSE stream.
  They were not in `EventType` or the event contract in `api/agent.py`, and the UI did not show
  them. They are now documented and rendered in the Streamlit step log.
- **Docker:**
  - `.dockerignore` lets through `benchmarks/metrics/*.yaml` only, so data, tasks and the
    generator stay out of the image.
  - The `api` stage copies the metrics to `/app/benchmarks/metrics` and sets
    `AGENT_SEMANTIC_METRICS_DIR`.
  - `docker-compose.yml` passes the three feature flags from `.env` or the shell (default
    `false`).
- **CI:** the `eval-smoke` job gains a third step, a dry run with routing, semantic layer and
  approval all on. The row threshold is 0, so every `SELECT` is gated and auto-approved.
- **Docs:** `README.md` and `docs/architecture.md` were rewritten to describe the merged system
  and share an identical mermaid diagram. Both render with mermaid 11.4.1 in headless Chromium.
  They cover:
  - the flags, CLI and eval options
  - every endpoint, including `/jobs/{id}/approve` (there is no `/datasets/samples` endpoint in
    any branch)
  - SSE events, the MCP server and prompt versioning
  - a step-by-step run

  `CLAUDE.md` gains the single-`Settings` rule and names the MCP server as a per-job sandbox user.
  `deploy/cloudrun.md` and `benchmarks/README.md` mention the flags and the image's metrics.

### Verification

- **Full suite:** `uv run pytest` gives 520 passed, 0 skipped. `ruff check`,
  `ruff format --check`, `uv lock --check` and `uv sync --locked --all-extras` all pass.
- **The three eval-smoke commands, run locally:** the oracle scores 100% (1 skipped) and exits 0.
  The dry run completes 3/3 and exits 0. The dry run with all flags on completes 3/3 (routed,
  metrics retrieved, gates auto-approved) and exits 0.
- **`eval run --agent dry-run --limit 3`** with `AGENT_ROUTING_ENABLED`,
  `AGENT_SEMANTIC_LAYER_ENABLED` and `AGENT_APPROVAL_ENABLED` all `true`: 3/3 tasks `ok` /
  `completed`, no errors or timeouts, exit 0, also with `--fail-on-error`. Without `--no-score`
  the scripted answers score as FAIL, which is expected: they are not analyses.
- **`analyst-agent ask --dry-run`** works with every flag combination, set by CLI flags (pytest
  matrix plus a manual run) and by `AGENT_*` env vars, including with two `--data` files.
- **UI end to end** (`scripts/ui-e2e/`): headless Chromium drives the real Streamlit app against
  the real API, `AgentLoop`, sandbox and `ApprovalBroker`, with the LLM scripted and all three
  flags on. It runs twice:
  - **Approve:** the job succeeds; the API records `approved: true, decided_by: human`.
  - **Deny with a note:** the API records `approved: false, decided_by: human` with the note; the
    model sees the denial as an error tool result.

  This ran both locally and against the `docker compose` stack: the frontend container, plus the
  API container running the same harness.
- **Compose with real `core_runner` and all flags on:** healthy. A job with no API key:
  - the router falls back to L3
  - semantic retrieval finds `churn_rate` and related metrics in `/app/benchmarks/metrics`
  - the `final` event carries prompt versions
  - the job then fails at the first agent call, as expected without a key

### Still inconsistent / worth a look

- **The Docker build could only be checked with workarounds.** In this environment ghcr.io blob
  downloads are blocked and the build container does not trust the egress proxy's CA. So I built
  from a scratch copy of the Dockerfile that used the host's `uv` 0.11.32 binary (the same
  version) and the proxy CA. The committed Dockerfile is unchanged apart from the metrics `COPY`
  and env. A plain `docker compose up --build` on a normal network was not run here.
- **No real-LLM run.** There is no `ANTHROPIC_API_KEY` here, so routing, the semantic layer and
  approval were only exercised with the scripted LLM. Live A/B evals are still to do: routing on
  vs off, semantic layer on vs off.
- **The dry run ignores denials.** The script is planned up front, so after a denied `run_sql` it
  still makes the chart and report from numbers it pre-computed. The denial reaches the model as
  an error result, but the dry run cannot show a re-plan. `--no-auto-approve` in eval likewise
  only checks plumbing.
- ~~The judge without a key counts as a failure, not a skip~~ — resolved in
  `fix/review-findings`: with no `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`, `eval run` warns
  and runs judge-less (L3 skipped, ADV rules only), and `LLMJudge` raises `JudgeUnavailable`,
  which the scorers record as skipped.
- ~~The semantic layer has no eval CLI flag~~ — resolved: `eval run --semantic-layer/
  --no-semantic-layer`, recorded in `meta.json` as `config.semantic_layer_enabled`.
- ~~`AGENT_SEMANTIC_METRICS_DIR` is relative to the working directory~~ — resolved: the
  default is now unset, meaning the definitions shipped with the package (the wheel carries
  `benchmarks/metrics` as `analyst_agent/semantic/metrics`; a source checkout reads
  `<repo>/benchmarks/metrics`). Explicit relative paths still resolve against the cwd.
- **`prompt_versions()` is static.** It lists every registered prompt, including
  `router` and `semantic_layer_rules` when those flags are off. It records what the code
  contains, not which prompts a run used; the run's flags are in the trace and the report.
- **The frontend's `AGENT_API_URL` and `AGENT_API_ID_TOKEN` are not in `Settings`.** The UI image
  ships without the agent package, so they are read with `os.environ`. They are documented in
  their own table in `docs/configuration.md`. They predate this round.
- **The UI e2e is not in CI.** It needs a browser and two servers; it is a manual script in
  `scripts/ui-e2e/`.
- **A known limitation from #11 remains.** DuckDB prints no cardinality for some operators (for
  example `Cross Product`), so the row gate uses the largest child estimate and can understate.
- **The dry-run heuristic is unchanged** (see round 1): it uses only the first table with several
  `--data` files.
- **Minor UI nit:** the collapsed status box above the approval panel shows a check icon while it
  reads "Waiting for your approval…".
- **Unchanged from round 1:** `httpx` is listed twice (harmless), `docs/kickoff/*.md` is
  historical, and the README Results table is empty.

---

## Round 1: #5, #2, #3, #4, #1

Merged into `main` in this order, running `uv sync --all-extras`, `uv run ruff check`,
`uv run ruff format --check` and `uv run pytest` after each step (all green before moving on;
final suite: 309 passed, 0 skipped).

| Order | PR | Branch | Result |
|---|---|---|---|
| 1 | #5 | `feat/sandbox` | clean merge |
| 2 | #2 | `feat/agent-core` | 1 conflict + 2 test fixups |
| 3 | #3 | `feat/eval` | clean merge, but its core adapter was broken (see below) |
| 4 | #4 | `feat/api-deploy` | `uv.lock` conflict; its core runner was broken too; e2e test un-skipped |
| 5 | #1 | `docs/architecture` | `README.md` conflict |

### Conflicts resolved

#### `src/analyst_agent/sandbox/__init__.py` (#2 agent-core vs #5 sandbox)
- Kept the real implementation from `feat/sandbox` in full and dropped agent-core's stub.
- Dataclass fields are unchanged and in the same order. `duration_ms` is **`int`** (the stub
  had `float`).
- `tests/test_sandbox_contract.py` (agent-core) now pins field names **and** annotations,
  including `duration_ms: int`, matching `tests/sandbox/test_interface.py`, and checks
  `isinstance(res.duration_ms, int)`.
- Two agent-core tests depended on the stub's exact output. They now check the real sandbox's
  output:
  - `tests/test_loop.py`: rejected `DROP` now checks for the `StatementNotAllowed` error kind
    instead of the stub's "Only SELECT" wording.
  - `tests/test_tools.py::test_run_sql`: the real `markdown_preview` has no `"N row(s)"` header.
    The test now checks that the preview starts with the table header and that
    `data["row_count"] == 2`.

#### `uv.lock` (#4 api-deploy)
- Took `main`'s side and regenerated it with `uv lock`. Not merged by hand.

#### `pyproject.toml`
- No textual conflict. The merged file keeps every group: `scipy` from the sandbox in
  `[project].dependencies`, the `api` and `frontend` optional extras from api-deploy, and the
  `dev` dependency group. `uv lock --check` and `uv sync --locked --all-extras` pass.

#### `README.md` (#1 docs vs #4 api-deploy)
- Base is the `docs/architecture` version. The **Serving (API + UI)** section from api-deploy
  now sits right after Quickstart and replaces the "once the API layer lands…" placeholder.
- I also fixed statements the merge had made wrong:
  - status banner and roadmap checkboxes
  - eval commands: the docs used `python -m analyst_agent.eval.runner …` and
    `… eval.report compare`, but the real CLI is `python -m analyst_agent.eval run|report|compare`
  - `uv sync --all-extras`
  - a link to `docs/sandbox.md`
  - the new `--dry-run` example

### Integration fixes (not textual conflicts)

- **The eval and API adapters were written against an older agent-core API.** Both imported
  `tools.create_sandbox`, `tools.sandbox_iface.table_name_for` and `ToolContext(sandbox=...)`,
  and agent-core had removed all three. Both run agents concurrently on threads, which the
  process-global module-level sandbox cannot support safely. Fix:
  - `ToolContext` has an optional per-run `sandbox`. Handlers call `ctx.run_sql` and
    `ctx.run_python`, which fall back to the module-level `sandbox.run_sql` and `run_python`
    when no sandbox is set, so the CLI and CLAUDE.md contract are unchanged.
  - `analyst_agent.tools` re-exports `create_sandbox` and `table_name_for` (moved out of
    `cli.py`).
  - The eval `core_agent` and the API `core_runner` create one sandbox per task/job, pass
    `artifacts_dir`, and close it afterwards.
  - New tests: per-run sandbox isolation (`tests/test_tools.py`) and the eval core adapter end
    to end (`tests/eval/test_core_adapter.py`).
- **The API end-to-end test is un-skipped.** `tests/api/test_integration_core.py` no longer
  uses `importorskip` and passes against the real loop, tools and sandbox.
- **CI** (`.github/workflows/ci.yml` from eval) ran `uv sync --locked` without extras, so the
  API tests could not import `fastapi`. It now runs `uv sync --locked --all-extras`.
- **`analyst-agent ask … --dry-run` is new.** `FakeLLM` moved from `tests/conftest.py` into
  `src/analyst_agent/core/fake_llm.py`, and `tests/conftest.py` re-exports it. `dry_run_llm()`
  scripts `inspect_schema → run_sql → make_chart → final_report` plus a passing critic, all with
  zero-token usage, and `tests/test_cli.py` covers it.

### Still inconsistent / worth a look

- **The dry-run column choice is a heuristic.** It groups by a text column the question mentions
  and sums a numeric column that is either mentioned or looks like revenue
  (`mrr`, `amount`, `price`, …). On `saas/subscriptions.csv` it gives a sensible answer
  (North America by MRR). On tables without a `region` column it picks arbitrary columns, for
  example `orders.csv` gives "completed has the highest total_unit_price". It checks the
  plumbing, not the analysis. With several `--data` files it only uses the first table.
- ~~Two ways to reach the sandbox / `RunState` vs `AgentState` / input formats differ~~ —
  resolved in `chore/consistency`: `ToolContext.sandbox` is documented in CLAUDE.md and
  `docs/architecture.md`, the diagrams say `AgentState`, and CLI, API, README and docs all list
  CSV, Parquet, SQLite and DuckDB (the sandbox gained DuckDB-file loading there).
- **`httpx` appears twice.** It is in the `dev` dependency group and in the `frontend` extra.
  This is harmless.
- **`docs/kickoff/*.md` still describe the pre-merge plan,** including the stubs. They are
  historical, so I left them unchanged.
- **The README Results table is empty.** No benchmark run has been done, and a real run needs
  `ANTHROPIC_API_KEY`.
