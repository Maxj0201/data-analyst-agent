# Final review: a cold read as a portfolio project

A senior engineer's read of the repository at `3071120` (v0.1.0), done the way a hiring
manager's reviewer would do it: clone fresh, follow the README quickstart word for word, check
that every claimed number comes out of the command printed next to it, then look at what a
reader notices in the first ten minutes. The fixes are on `chore/final-review`. Anything that
takes more than about an hour, or that is the owner's decision, is listed under
[Not fixed](#not-fixed) with the reason.

Inputs read first: `CLAUDE.md`, `CHANGELOG.md`, [`review.md`](review.md),
[`review-2.md`](review-2.md) and every round of [`integration-notes.md`](integration-notes.md).

## Verdict

The engineering is strong and unusually well evidenced: a kernel-enforced sandbox with two
hostile reviews and a fuzzer behind it, an 80-task benchmark with an oracle self-check, and an
offline CI that exercises the real loop. Every offline number in the README reproduced exactly
from a fresh clone. The README contradicted itself in one place: its first paragraph said
every change "is gated by an 80-task benchmark", while its *Results* section says, correctly,
that no model has been scored. That sentence is fixed. The biggest remaining gap is the one the
repo already admits: there is no live-model result. A reader will find that empty table before
anything else. Second comes provenance (see [N2](#not-fixed)).

## Quickstart, followed literally

Fresh `git clone` into an empty directory, Python 3.11 picked by `.python-version`, uv 0.11.

| # | Step as written in the README | Result |
|---|---|---|
| 1 | `git clone … && cd data-analyst-agent` | Works. (Cloned through this session's proxy, so I could not check whether the repo is public. If it is private, a reader cannot get past step 1.) |
| 2 | `uv sync --all-extras --no-extra embeddings` | Works, no warnings from the project. |
| 3 | `uv run analyst-agent ask --dry-run --data …subscriptions.csv "which region has the highest mrr"` | Works, exit 0. Correct answer (North America, 1,290,124), a table, the SQL and a chart in `./artifacts/`. |
| 4 | `uv run python -m analyst_agent.eval run --agent oracle --no-judge --limit 5 --fail-under 1.0` | Works, exit 0. 4 of 4 gradable tasks pass; the L3 task shows as skipped, which is expected without a judge. |
| 5 | `export ANTHROPIC_API_KEY=…` | Not run: no key in this environment. |
| 6 | `uv run analyst-agent ask -v --data …orders.csv "…"` | Not run live. **Run without the key (the easiest mistake to make here), it printed a full report whose status read `TypeError: Could not resolve authentication method…`, and exited 1.** Fixed ([F2](#fixed)). |
| 7 | `scripts/demo.sh` | Not verifiable: no Docker daemon here. The script detects that and says so in one line. |

## Claimed numbers, reproduced

Every row of the README's offline table was re-run from the fresh clone with the command shown
next to it.

| Claim | Command | Reproduced |
|---|---|---|
| 1001 passed, 1 slow deselected | `make coverage` (same suite as `make test`) | 1001 passed, 1 deselected. 1002 with this PR's new test. |
| Coverage 94.2% / `sandbox/` 99.4% / `core/loop.py` 100% | `make coverage` | 94.2% / 99% / 100%, and all three floors pass. |
| Lint and format clean | `make lint` | Clean. |
| Oracle 100% on the 64 gradable tasks | `eval run --agent oracle --no-judge` | 24/24 L1, 24/24 L2, 16/16 ADV, 16 L3 skipped. |
| BM25 1.000 / 1.000 / 1.000 on 33 cases | `python -m analyst_agent.semantic eval` | 1.000 / 1.000 / 1.000. Paraphrases 0.600 / 0.900 / 0.758, matching the CHANGELOG. |
| 265 sandbox tests | `pytest tests/sandbox` | 265 collected; they pass as part of the full suite. |
| Judge calibration plumbing κ 1.000 | `eval judge-calibrate --fake` | 100% agreement, κ 1.000. |
| Fuzzer 500/500, UI e2e 24/24, Docker image sizes | `make fuzz`, `make e2e`, `docker build` | Not re-run: no Docker daemon here, and the fuzzer runs nightly. Taken from the integration notes. |

## Fixed

| ID | Finding | Fix |
|---|---|---|
| F1 | **Scratch files in the tree.** `traces/` held 32 `run_*.json` span trees (540 KB, 15k lines), committed by accident with the round-4 integration notes (`0f2a5cd`). `traces` is the default `AGENT_TRACES_DIR`, relative to the working directory, and it was not in `.gitignore`. | Removed. `traces/` is now in `.gitignore` and `.dockerignore`. |
| F2 | **The quickstart's live step failed badly without a key.** `ask` without `--dry-run` ran the loop and printed a report scaffold containing an SDK `TypeError`. | `ask` checks for `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` first and exits 2 with one line pointing at the key or `--dry-run`. Regression test: `tests/test_cli.py::test_ask_without_credentials_says_so_instead_of_running`. |
| F3 | **The README's first paragraph overclaimed.** It said "every change to it is gated by an 80-task benchmark". CI actually gates on the oracle over 5 tasks plus scripted dry runs, and no model has been scored. | It now says the project ships with the benchmark and offline gates, and that it has not been scored against a live model yet, linking to *Results*. |
| F4 | **"Current release: v0.1.0", but there is no git tag.** `git tag` is empty, so the CHANGELOG's `releases/tag/v0.1.0` link is a 404. | The README now says "Current version: 0.1.0". Pushing the tag is left to the owner ([N3](#not-fixed)). |
| F5 | **Duplicated code (review L21).** There were five copies of SQL identifier quoting (`tools/handlers`, `core/router` inline, `core/memory`, `core/fake_llm`, `sandbox/_loaders`) and two copies of the file-to-table-name rule (`tools.table_name_for`, `sandbox._loaders.safe_identifier`). | `sandbox` exports `quote_ident` and `safe_identifier`, and every caller uses them. `table_name_for` delegates to `safe_identifier`. Behaviour is unchanged: the full suite passes and the dry-run SQL is byte-identical. |
| F6 | **Dead code (review L22).** The `sandbox.current_tables` alias had no callers, `_read_roots(work_dir)` ignored its parameter, and `_ms_since` was defined twice. | Removed. There is one `ms_since`, in `duckdb_runner`. |
| F7 | **Stale facts in `interview-notes.md`.** It said "four merge rounds" twice; there were six (PRs #1–#27). | Corrected. |
| F8 | **`docs/history/kickoff/` was unlabelled.** It holds the four original task briefs, which still describe the pre-merge stubs. A reader browsing `docs/` could take them for current docs. | Added `docs/history/kickoff/README.md`, which says they are historical and where the current docs start. |
| F9 | The README's test count went stale with F2's new test. | 1001 → 1002 in the README. The CHANGELOG keeps 1001, since it records the counts at the tag. |

Checked and found clean:

- **TODO / FIXME / XXX / HACK:** none in `src/`, `tests/`, `scripts/`, `frontend/` or the docs.
- **Tests that test nothing:** an AST scan of every `test_*` function found 5 without a
  literal `assert`. Four call the `_blocked(...)` helper, which asserts. The fifth,
  `tests/api/test_observability.py::test_tracer_errors_never_propagate`, tests that a call does
  not raise, which is a real contract.
- **Other scratch files:** none. Every tracked file outside the package directories is a
  standard project file. The largest tracked files are `uv.lock` and the benchmark CSVs.
- **Naming:** consistent. Settings use the `AGENT_*` prefix, the CLI, package and Docker targets
  agree, and the one exception (the frontend's `AGENT_API_URL`) is already documented as open
  item 17.

## Not fixed

Ordered by how much a reader will notice each one.

| ID | Finding | Why not here / what to do |
|---|---|---|
| N1 | **No live-model numbers.** The README *Results* table and the runbook's results are empty, so nothing shows the agent answering a question correctly with a real model. | Needs an API key and about $143 (capped at $214) for the 8 arms. A cheaper first step that would change the reader's impression: one Sonnet baseline over all 80 tasks (`scripts/run_experiments.sh --print-commands` shows the arm), published with its `report.md`. |
| N2 | **Provenance is not stated.** `CLAUDE.md`, `docs/history/kickoff/`, the integration notes (which mention "the session proxy") and the commit trailers make clear that parallel AI coding sessions wrote the code under the owner's direction. The README does not say so, and a reviewer will work it out within minutes. | It is the owner's statement to make, so I have not written it. My recommendation: add a short "How this was built" section to the README that names the process: the written interface contract, parallel sessions, integration rounds, and two hostile reviews. The process is a strength of the repo, and it reads better stated than discovered. |
| N3 | **There is no `v0.1.0` tag**, so the CHANGELOG's release link is dead. | `git tag -a v0.1.0 3071120 -m "v0.1.0" && git push origin v0.1.0`, or tag the merge commit of this PR. Pushing tags is outside this PR. |
| N4 | **Process documentation outweighs product documentation.** `integration-notes.md` (1,077 lines), `review.md`, `review-2.md` and `interview-notes.md` come to about 2,100 lines, against a 309-line README. The name `interview-notes.md` reads like rehearsed answers. | I suggest renaming it `design-decisions.md` (its own title is "design decisions and integration stories") and adding a five-line "where to start" list to `docs/`. The rename touches links in 6 files and is the owner's call. |
| N5 | **Stale branches.** A fresh clone shows 8 remote branches: six `claude/*` session branches and `docs/demo-assets`, all merged. The integration notes' list of undeleted branches is itself out of date (most of the ten it names are already gone). | The session proxy refuses `git push --delete` (round 5). The owner should delete them with repository access. |
| N6 | **Hard-coded test counts** in the README and CHANGELOG go stale with every PR; this one had to bump them. | Say "1000+ tests", or have CI print the count. The coverage percentages are fine, because CI gates on their floors. |
| N7 | **Remaining duplication from review L21:** three "JSON object from model text" parsers (`critic`, `router`, `eval/judge.parse_json_object`), two Markdown table renderers (`tools/formatting.rows_to_markdown`, `duckdb_runner.to_markdown`), two `_jsonable` helpers, and `ModelUsage` / `CostTracker`. | Their edge-case behaviour differs (fence stripping, which fallbacks they try), and the critic and router parsers feed the prompt-version tests. Merging them deserves its own PR with characterisation tests first, which is more than an hour. |
| N8 | **Large modules:** `core/loop.py` (810 lines), `core/tracing.py` (769), `eval/runner.py` (769), `eval/scorers.py` (702), `sandbox/_child/_oslock.py` (612). | All are sectioned and readable; none is a dumping ground. `tracing.py` splits naturally into recorder, renderer and store, and is the first candidate. |
| N9 | **Plain `uv sync` and then `uv run pytest` fails.** Several tests need packages that only arrive with extras: for example, pyarrow, which comes in only through `streamlit`. | The README, `make install` and CI all use `--all-extras`, so the documented path works. Either list `pyarrow` in the dev group or `importorskip` the affected tests. |
| N10 | **"`httpx` is listed twice"** (integration-notes housekeeping). | Not a defect, so I am closing it: it is a runtime dependency of the `frontend` extra and a dev dependency for FastAPI's `TestClient`. |
| N11 | Open items 1–18 in [integration-notes.md](integration-notes.md#still-open-at-v010) and the accepted Low findings of both reviews. | Unchanged by this PR, except review L21 and L22, which F5 and F6 partly close. |

## Verification of this PR

- `make lint`: clean.
- `make coverage`: the full suite (1002 passed, 1 slow deselected) and all three coverage
  floors.
- The dry-run quickstart (step 3) gives the same answer and the same SQL as before.
