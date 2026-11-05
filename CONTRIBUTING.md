# Contributing

Thanks for helping. This file covers the workflow, the rules that keep parallel branches
mergeable, and the checks a PR has to pass. The project conventions are in
[`CLAUDE.md`](CLAUDE.md) and apply to humans too.

## Setup

Requires Python 3.11 and [uv](https://docs.astral.sh/uv/). Node 22 is needed only for the UI e2e
test, and Docker only for `make demo`.

```bash
make install    # uv sync --locked --all-extras --no-extra embeddings
```

```bash
make help       # every target
make hooks      # optional: install the pre-commit hooks (below)
```

Add `--extra embeddings` only when working on the embedding or hybrid semantic backends. It pulls
in torch, and the tests mock the encoder.

## Before you open a PR

```bash
make check      # install + lint + format check + tests + coverage gate + offline eval gate
```

| Target | What it runs | Needs |
|---|---|---|
| `make hooks` | installs the pre-commit hooks (see below) | — |
| `make lint` | `ruff check` + `ruff format --check` (line length 100) | — |
| `make format` | applies `ruff format` and autofixable lint fixes | — |
| `make test` | `pytest` with the API key blanked and the base URL pointed at a dead port | — |
| `make coverage` | `make test` with branch coverage, then CI's gate: overall ≥ 92 %, `sandbox/` ≥ 97 %, `core/loop.py` ≥ 98 % | — |
| `make fuzz` | the sandbox escape fuzzer (`-m slow`, 500 snippets, default seed) | — |
| `make eval` | CI's eval-smoke: oracle at 100%, a dry run of the real loop, a dry run with every flag on | — |
| `make e2e` | headless Chromium drives the Streamlit UI against the dry-run API (approve and deny) | Node + `playwright` |
| `make eval-live` | a real benchmark run (`EVAL_ARGS="-d L1 --limit 5"`) | `ANTHROPIC_API_KEY`; costs money |
| `make demo` | `docker compose` stack with sample datasets | Docker |

CI runs `make lint`, `make coverage` and `make eval` on every PR (the workflow calls the Makefile,
so `make check` is what CI runs: change commands in the Makefile, not in the workflow), and the
fuzzer nightly (`.github/workflows/nightly.yml`). Tests marked `@pytest.mark.slow` are deselected
by default (`addopts` in `pyproject.toml`) and are not part of the coverage measurement, so every
line in `src/` must be reachable from a fast test. Property tests (Hypothesis) live in
`tests/property/`; they are deterministic by default (profile `ci`, 150 examples), and
`HYPOTHESIS_PROFILE=dev` runs 1000 random examples each. `ui-e2e` runs on PRs that touch `frontend/`,
`src/analyst_agent/api/`, `core/approval.py`, `core/fake_llm.py` or `scripts/ui-e2e/`.

## Pre-commit hooks

`make hooks` installs [`.pre-commit-config.yaml`](.pre-commit-config.yaml) (via `uvx pre-commit`).
On each commit it runs `ruff check --fix`, `ruff format`, `uv lock --check` when `pyproject.toml`
or `uv.lock` change, and `scripts/check_prompt_versions.py` when `core/prompts.py` or
`eval/scorers.py` change. The last one compares every registered prompt with `HEAD` and fails if
the text changed but the version did not; use `--base origin/main` to compare a whole branch.
The hooks run through `uv run --locked`, so they use the same ruff as CI.

## Rules

**Tests never call the real API.** Mock the LLM with `FakeLLM` (`core/fake_llm.py`, re-exported
from `tests/conftest.py`). It has separate queues for the agent, critic, report writer and
router.

**All LLM calls go through `core/llm.py`.** This covers new roles (a judge, a classifier) too,
so that cost accounting, retries and prompt caching apply.

**Configuration.** Every `AGENT_*` setting is a field on `core/config.py:Settings`, with a
default, a row in `docs/configuration.md` and a line in `.env.example`. `tests/test_config.py`
enforces all three. There are no other settings classes.

**New features go behind a flag.** Use an `AGENT_<FEATURE>_ENABLED` flag that defaults to
`false`. With the flag off, behaviour must stay byte-for-byte the same: same prompts, same tool
schemas, no extra calls. Add the flag to the `ask --dry-run` matrix in `tests/test_cli.py` and to
the "every flag on" step in `.github/workflows/ci.yml` (and the Makefile's `eval` target).

**Prompts are versioned.** If you change the text of any prompt in `core/prompts.py`, including
the shared untrusted-data notice:

1. bump its version;
2. update the pinned fingerprint in `tests/test_prompts.py`;
3. put an `eval compare` of before vs after in the PR description when the change can affect
   answers.

**Sandbox access.** The CLI uses the module-level `sandbox.run_sql` / `run_python`. Anything
concurrent (API jobs, eval tasks, the MCP server) creates its own sandbox with
`tools.create_sandbox(...)`, passes it as `ToolContext(sandbox=sb)` and closes it. Tool
handlers call `ctx.run_sql` / `ctx.run_python`, never the module-level functions directly.

**Package boundaries.** Code lives in `src/analyst_agent/{core,tools,sandbox,eval,api,semantic}`.
Stay inside your area; if you must edit outside it, say so in the PR description.

## Security checklist

Every item here is a review finding that was fixed and then came back in a later branch (see
[`docs/interview-notes.md`](docs/interview-notes.md#c-security-fixes-regressed-by-branches-written-before-them)).
Check each one that applies:

- [ ] **Anything from a dataset, a tool result or stored memory that reaches the model goes
      through `wrap_untrusted()`, or a fence built the same way.** Delimiters must be
      neutralised case- and whitespace-insensitively. A new `<block>` needs its own test with
      `</BLOCK>` and `< /block>` in the data. (M1)
- [ ] **Untrusted Markdown in the UI goes through `frontend/render.py`** (`safe_md`,
      `code_span`, `report_parts`). Never pass it to `st.markdown` / `st.error` / labels
      directly. Images are rendered only for this job's own `/artifacts/...` URLs. (M2)
- [ ] **Code that runs in the API process is thread-safe.** No pyplot or other global state, and
      model-controlled sizes are bounded. (M4)
- [ ] **Every `tool_use` gets exactly one `tool_result`,** including on error paths. (M5)
- [ ] **Approval gates fail closed** on unparseable input (comments, `EXPLAIN ANALYZE`). (M6)
- [ ] **No `preexec_fn` and no subprocesses from the API process** outside the sandbox
      runner. (M7)
- [ ] **Do not weaken `sandbox/_child/_oslock.py`** (the seccomp and Landlock layers) without
      updating `docs/sandbox.md`, the escape tests in `tests/sandbox/` and the rule checks in
      `tests/sandbox/test_oslock_unit.py`, and running `make fuzz`. (H1, H2)

## Repository layout notes

- `docs/history/kickoff/` holds the four session briefs the project was started from. They are
  **historical**: they describe the pre-merge plan (including stubs that no longer exist), so do
  not treat them as documentation or update them. The current design is in
  `docs/architecture.md`.
- Run outputs (`runs/`, `traces/`, `artifacts/`, `*.sqlite3`) and images outside `docs/assets/`
  are git-ignored. If a doc needs an example run, copy the relevant part into `docs/assets/`.
- Security reports go through [`SECURITY.md`](SECURITY.md), not public issues. The project is
  [MIT licensed](LICENSE).

## Commits and PRs

- Work on a branch named after the task (`feat/...`, `fix/...`, `docs/...`) and commit in small
  logical steps.
- Fill in the PR template (`.github/PULL_REQUEST_TEMPLATE.md`). The description says what changed, how you verified it (commands and results) and anything
  you edited outside your area.
- If your change affects documented behaviour, update `README.md` and the matching `docs/*.md`
  in the same PR. `docs/architecture.md` describes `main`. If you change the diagram, check that
  it still renders: mermaid 11.4.1, the version GitHub uses.
