# Integration notes

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

## Conflicts resolved

### `src/analyst_agent/sandbox/__init__.py` (#2 agent-core vs #5 sandbox)
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

### `uv.lock` (#4 api-deploy)
- Took `main`'s side and regenerated it with `uv lock`. Not merged by hand.

### `pyproject.toml`
- No textual conflict. The merged file keeps every group: `scipy` from the sandbox in
  `[project].dependencies`, the `api` and `frontend` optional extras from api-deploy, and the
  `dev` dependency group. `uv lock --check` and `uv sync --locked --all-extras` pass.

### `README.md` (#1 docs vs #4 api-deploy)
- Base is the `docs/architecture` version. The **Serving (API + UI)** section from api-deploy
  now sits right after Quickstart and replaces the "once the API layer lands…" placeholder.
- I also fixed statements the merge had made wrong:
  - status banner and roadmap checkboxes
  - eval commands: the docs used `python -m analyst_agent.eval.runner …` and
    `… eval.report compare`, but the real CLI is `python -m analyst_agent.eval run|report|compare`
  - `uv sync --all-extras`
  - a link to `docs/sandbox.md`
  - the new `--dry-run` example

## Integration fixes (not textual conflicts)

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

## Still inconsistent / worth a look

- **The dry-run column choice is a heuristic.** It groups by a text column the question mentions
  and sums a numeric column that is either mentioned or looks like revenue
  (`mrr`, `amount`, `price`, …). On `saas/subscriptions.csv` it gives a sensible answer
  (North America by MRR). On tables without a `region` column it picks arbitrary columns, for
  example `orders.csv` gives "completed has the highest total_unit_price". It checks the
  plumbing, not the analysis. With several `--data` files it only uses the first table.
- **There are two ways to reach the sandbox.** One is the module-level functions from
  CLAUDE.md, used by the CLI. The other is the per-run `ToolContext.sandbox`, used by the API
  and eval. CLAUDE.md only describes the first; consider documenting the second.
- **`RunState` vs `AgentState`.** The architecture diagram (README and `docs/architecture.md`)
  says `RunState`, but the code class is `core.loop.AgentState`.
- **Input formats differ.** The CLI `--data` help says "CSV/Parquet". The sandbox also loads
  SQLite, but `table_name_for` gives one name per file while a SQLite file expands to one table
  per SQLite table. The API upload only accepts `.csv`. The README and architecture doc mention
  CSV / SQLite / DuckDB.
- **`httpx` appears twice.** It is in the `dev` dependency group and in the `frontend` extra.
  This is harmless.
- **`docs/kickoff/*.md` still describe the pre-merge plan,** including the stubs. They are
  historical, so I left them unchanged.
- **The README Results table is empty.** No benchmark run has been done, and a real run needs
  `ANTHROPIC_API_KEY`.
