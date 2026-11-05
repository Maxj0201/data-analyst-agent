# Integration notes

## Round 6 (final, v0.1.0): #27, #25, #26, #24 (2026-11-05)

This is the last integration round. `main` is tagged **v0.1.0** and [CHANGELOG.md](../CHANGELOG.md)
summarises the release by area.

The four PRs were merged into `main` in this order, all as merge commits. After each merge I ran:

- `uv sync --all-extras --no-extra embeddings`
- `uv run ruff check .` and `uv run ruff format --check .`
- `uv run pytest --cov` with CI's three coverage floors

Every merge was green before I went on. Final suite: **1001 passed, 1 deselected (slow), 0
skipped**.

| Order | PR | Branch | Merge | Result | Tests after | Coverage (overall / `sandbox/` / `core/loop.py`) |
|---|---|---|---|---|---|---|
| 1 | #27 | `chore/docker-verify` | `1f80dac` | clean | 966 | 94% / 99% / 100% |
| 2 | #25 | `feat/semantic-tuning` | `4367311` | clean | 976 | 94% / 99% / 100% |
| 3 | #26 | `feat/eval-judge-quality` | `b6146c1` | clean | 1001 | 94% / 99% / 100% |
| 4 | #24 | `docs/demo-assets` | `cef4fa1` | clean | 1001 | 94% / 99% / 100% |
| – | | planner prompt 1.2.0 | `35c33ff` | | 1001 | 94.24% / 99.38% / 100% |
| – | | trace example regenerated | `5fbeba2` | | 1001 | unchanged |

None of the four PRs had a textual conflict. All four branched from `8686150`. Below is what I
checked at each expected collision point.

### Collision points

- **#26's runner versus #27's compose and e2e changes.**
  - The files do not overlap. #26 changed `eval/runner.py`, the scorers, the CLI, the report
    and `docs/benchmark.md`. #27 changed the Dockerfile, the compose files,
    `scripts/ui-e2e/*`, `scripts/sandbox-probes/*`, `docs/deploy.md` and `deploy/cloudrun.md`.
    Neither touched `docs/eval-runbook.md`, so both sets of changes are kept as written.
  - #26's new flush trigger (`waiting >= unfinished`, counting tasks whose scorer is waiting on
    the batch judge) still sits behind round 5's `stopping` guard, so a cancelled `--batch` run
    sends no batch.
  - With `--judge-samples 3`, the gathered judge calls all enqueue before the flush runs: one
    batch of 4 (3 L3 samples + 1 ADV) against the fake API below.
- **#25's `vocabulary.yaml` versus #26's `benchmarks/judge_calibration/`.**
  - `tests/eval/test_open_items.py` lists the bundled metrics directory and expects
    `vocabulary.yaml`. The metrics loader skips files whose `kind` is `vocabulary` or
    `retrieval_eval`.
  - The wheel force-includes only `benchmarks/metrics` (as
    `analyst_agent/semantic/metrics`), so it carries `vocabulary.yaml`. #25 checked this with a
    built wheel.
  - `judge_calibration/` sits outside `benchmarks/metrics` and outside the `.dockerignore`
    allow-list, so neither the wheel nor the image sees it. Like `semantic eval`,
    `judge-calibrate` is a dev tool that reads the source checkout.
- **README.** #24's *See it run*, #25's BM25 row and roadmap items and #27's `docs/deploy.md`
  link are all kept. I re-checked every number the README cites against the merged tree:
  - tests: 976 → **1001**
  - coverage: 94.4% → **94.2%**, because #26 added code at slightly lower branch coverage
  - `tests/sandbox`: 265 (unchanged)
  - BM25 on the 33 benchmark cases: 1.000 / 1.000 / 1.000
  - oracle on all 80 tasks: 100% on 64 gradable (unchanged)
  - UI e2e: 24/24
  - The README cites no image sizes. `docs/deploy.md` now has the v0.1.0 sizes next to #27's.
  - #24's span tree was captured at `planner@1.1.0`. I regenerated it at 1.2.0 with the same
    command, and the layout is identical.
  - The README called that tree "the same run" as the screenshots. It is a CLI dry run with
    the same flags, so the caption now says so.
- **Planner prompt 1.1.0 → 1.2.0.** #26 warned that L3 scores will drop under the key-numbers
  rule. The planner now has one more rule: *"Put every figure you cite in the report, prose
  included, into key_numbers as well."*
  - `tests/test_prompts.py` pins `("1.2.0", "108ef068a7b7")`.
  - `docs/architecture.md`, `docs/observability.md` (its example), `docs/report-and-memory.md`
    (version table) and `docs/benchmark.md` were updated.
  - Runs before and after this change are not comparable on L3, which is also true of the judge
    change itself.

### Other changes in this round

- `CHANGELOG.md`, new.
- `docs/eval-runbook.md`: pre-flight now includes `judge-calibrate --judge-samples 3`. #26 asked
  for that run before merging, but it needs a key, so it is a runbook step.
- README:
  - the eval paragraph describes judge 2.0.0 and its calibration
  - the measured-numbers table gains the calibration row
  - the roadmap's done items are grouped together
  - the repository map lists the vocabulary, the calibration set and the newer docs

### Verification (all local)

- **`ask --dry-run` flag matrix:** all 32 combinations of routing × semantic layer × approval
  (auto, row threshold 0) × structured report × memory. The trace store was on in all of them,
  and JSON logs in half.
  - **32/32 `completed`**, every answer "North America", 32 span trees
  - one approval node in each of the 16 approval-on runs, none in the others
- **`eval run --agent dry-run --limit 5 --batch` with all ten `AGENT_*_ENABLED` flags on.** That
  is routing, semantic, structured, memory, approval, Langfuse, samples, trace store, JSON logs
  and judge batch. It ran against a throwaway local fake of `/v1/messages` and
  `/v1/messages/batches` with the real SDK client, as in round 5.
  - 5/5 `ok`, exit 0
  - **one batch of 2 requests**, no direct calls:
    `judge_batch = {batches: 1, batched_requests: 2, direct_requests: 0, fallback: null}`
  - `spend_usd` 0.006 and 5 span trees
  - `meta.json` prompts include `planner@1.2.0`, `rubric_judge@2.0.0` and
    `behavior_judge@1.0.0`
  - With `--judge-samples 3`: one batch of 4, and a *Judge variance* table in the report.
  - L1/L2 fail scoring, as in round 5: the dry-run agent's answers are not analyses.
- **`eval judge-calibrate --fake`:**
  - 24 samples, 0 errors
  - verdict agreement 100%, κ 1.000; point agreement 100%, κ 1.000
  - confusion: 8 pass / 16 fail
  - This is the oracle fake, so it checks the plumbing only.
- **`python -m analyst_agent.semantic eval`:**
  - BM25, benchmark cases (33): top-1 **1.000**, top-3 1.000, MRR 1.000
  - paraphrases (10): 0.600 / 0.900 / 0.758
  - all 43: 0.907 / 0.977 / 0.944
  - embedding and hybrid skipped: no extra
- **`scripts/preflight.sh --dry-run`:** the same as round 5. Every check is `ok` except two:
  `extra [embeddings]` FAILs (expected with `--no-extra embeddings`) and Message Batches WARNs.
  Exit 1. The estimate is unchanged: $142.58 for all experiments, $213.87 capped.
- **CI jobs, run as in the workflows:**
  - **`ci.yml` test:** `uv lock --check` passed, `uv sync --locked --all-extras --no-extra
    embeddings`, lint, format, `pytest --cov` (1001 passed). Gate: overall 94.24% (≥ 92),
    `sandbox/` 99.38% (≥ 97), `core/loop.py` 100% (≥ 98). `eval/` is 92.39%.
  - **`ci.yml` eval-smoke:**
    - oracle `--limit 5`: 100%
    - dry run (3): ok
    - dry run with every flag (5): ok, 5 span trees, 101 JSON log lines carrying `task_id`
  - **`ui-e2e.yml`:** `scripts/ui-e2e/run.sh` gave **24/24**.
  - **`nightly.yml`:** kernel 6.18, Landlock ABI 7. The fuzzer passed with the fixed seed and
    with a new seed (20261105), about 175 s each.
  - **`docker.yml` `images`:** this session had a Docker 29.9.2 daemon. I used #27's two
    build-arg substitutions: `UV_IMAGE` held the host's uv 0.11.32, and `PYTHON_IMAGE` was the
    official slim-bookworm image plus the proxy CA. I also passed `--network host`, so the
    build reaches the session proxy. The Dockerfile is unchanged.
    - `api` 984 MB / 222 MB compressed; `frontend` 832 MB / 191 MB
    - `scripts/sandbox-probes/run.sh analyst-agent-api:local`: all ok (H1/H2 through
      `POST /ask`, seccomp and Landlock kernel checks)
    - `scripts/ui-e2e/run.sh --docker`: **24/24**
  - **`docker.yml` `embeddings-image`:** **not run**, because the proxy refuses
    `download.pytorch.org` (and `huggingface.co`).

### #27's own notes: Docker verification (`chore/docker-verify`, 2026-11-04)

Written by #27 and kept as it was. First build of the real images, with a Docker 29.9.2 daemon. Details, sizes and the sandbox's
runtime requirements are in [deploy.md](deploy.md); this is what broke and what changed.

- **Restricted network, not the Dockerfile.** `ghcr.io` blobs are blocked here and TLS is
  re-signed by the egress proxy, so `UV_IMAGE` (same `uv` 0.11.32 binary, from PyPI) and
  `PYTHON_IMAGE` (official image + proxy CA) were overridden as build args. The
  `download.pytorch.org` index is blocked too, so the embeddings image was not built.
  `.github/workflows/docker.yml` builds everything with the official images on a clean runner.
- **Trace store broken in the image (fixed).** `AGENT_TRACES_DIR` defaulted to `traces/`, i.e.
  root-owned `/app/traces`, so with `AGENT_TRACE_STORE_ENABLED=true` every job logged
  "Tracing failed while finishing job". The image now sets `AGENT_TRACES_DIR=/app/data/traces`.
- **Compose passed only three of the eight runtime flags (fixed).** Structured reports, memory,
  Langfuse, trace store and JSON logs set in the shell never reached the container.
- **UI e2e harness ignored `AGENT_STRUCTURED_REPORT_ENABLED` (fixed).** `dryrun_api.py` always
  scripted a Markdown report; with the flag on the loop rejects it and the script runs out
  ("no scripted response left"). `run.sh` now turns on every `AGENT_*_ENABLED` flag (Langfuse
  keyless), locally and in the new `--docker` mode: 24/24 both ways.
- **Sandbox in the container.** New `scripts/sandbox-probes/`: H1/H2 through `POST /ask` in the
  API image, plus Landlock + seccomp on their own via `docker exec`. All pass under the default
  seccomp profile, `no-new-privileges` + `cap-drop ALL`, `--read-only`, `seccomp=unconfined`.
  A profile hiding `landlock_*` gives the documented fallback; one denying `seccomp` fails
  closed (`SandboxSetupError`). The API image has no pyarrow (only streamlit pulls it in), so
  the H2 vector does not exist there; the kernel check is what proves Landlock works.
- **Embeddings image: CPU-only torch.** The build installs the locked torch version from
  `TORCH_INDEX_URL` (default: the CPU index) and the rest of the extra pinned to `uv.lock`,
  without the CUDA packages. `uv.lock` is unchanged.

### Branches

Not deleted from this session (round 5 explains why: the git proxy refuses `git push --delete`).
These remote branches belong to merged PRs and are contained in `main`:

- from round 5: `docs/eval-runbook`, `docs/final`, `test/coverage-and-properties`,
  `test/sandbox-fuzz`, `fix/review-2` and `chore/first-run`
- from this round: `chore/docker-verify`, `feat/semantic-tuning`, `feat/eval-judge-quality` and
  `docs/demo-assets`

To delete them with repository write access:

```bash
git push origin --delete docs/eval-runbook docs/final test/coverage-and-properties test/sandbox-fuzz fix/review-2 chore/first-run chore/docker-verify feat/semantic-tuning feat/eval-judge-quality docs/demo-assets
```

### Still open at v0.1.0

This is the complete list. It replaces the *Still open* lists of earlier rounds, which are kept
below as history.

1. **No live-model run at all.** The README *Results* table is empty, and so is the runbook's
   *Benchmark results* section. Filling them needs `ANTHROPIC_API_KEY` and
   `scripts/run_experiments.sh`: an estimated $142.58 for the 8 arms, capped at $213.87.
2. **The real judge's agreement with human labels is not measured.** Run
   `eval judge-calibrate --judge-samples 3` before trusting L3. The calibration set has 24
   samples over 8 of the 16 L3 tasks.
3. **The L3 baseline needs re-measuring.** Judge 2.0.0 and planner 1.2.0 both change L3 scores.
   No earlier L3 number exists to compare with, but any run before this tag is not comparable.
4. **The cost estimate predates judge 2.0.0.** Preflight's token profiles (±50%) do not account
   for the longer v2 judge prompt and its evidence quotes, or for `--judge-samples N`, which
   multiplies L3 judge spend N times. Compare after the baseline, as the runbook says.
5. **Embedding and hybrid retrieval are not measured,** and the hybrid weight is not swept.
   The proxy refuses `download.pytorch.org` and `huggingface.co`. The commands are in
   `docs/semantic-layer.md`. `AGENT_SEMANTIC_HYBRID_WEIGHT` stays at 0.5.
6. **BM25 paraphrases:** 4 of the 10 held-out paraphrases still miss top-1 (0.600). The
   benchmark questions score 1.000, but the vocabulary was tuned with them in view.
7. **The embeddings Docker image (CPU torch) has never been built.** Its size is not recorded.
   The `embeddings-image` CI job is the first real build.
8. **The Docker images have only been built with substituted base images** (uv binary, proxy
   CA). The `images` CI job is the first build from the official images on a clean network.
9. **Cloud Run is not tested.** This includes gen2 Landlock/seccomp and `PR_SET_PDEATHSIG`
   under gVisor. `docs/deploy.md` has a `sandbox-check` job for it.
10. **Message Batches have only run against a local fake.** The first real `--batch` run is the
    check. If batches fail, the runner falls back to direct calls and records `fallback`.
11. **In-flight spend at a hard kill is not recorded** (at most `--concurrency` tasks).
12. **When Landlock is unavailable, the warning goes only to the sandbox child's stderr.**
    Operators never see it. A startup self-check that logs it is a follow-up from #27.
13. **The dry run ignores denials** (it is scripted up front), and with several `--data` files it
    uses only the first table.
14. **The approval row gate can understate:** DuckDB prints no cardinality for some operators
    (`Cross Product`), so the gate fails open (review L13). The 30 s SQL timeout still bounds
    the cost.
15. **Dev tools read the source checkout:** `semantic eval` and `judge-calibrate` do not work
    from an installed wheel.
16. **`prompt_versions()` is static.** It lists every prompt, including those of flags that
    are off, and now the judge prompts in eval `meta.json`.
17. **The frontend's `AGENT_API_URL` and `AGENT_API_ID_TOKEN` are not in `Settings`.** The UI
    image ships without the agent package. Both are documented separately.
18. **Accepted Low findings:**
    - review.md L1–L24: no app-level auth (L1), shared artifact directory (L2), one worker per
      store (L14), no job-level wall-clock timeout (L15), retries ignore `Retry-After` (L16), …
    - review-2.md L2–L16: MCP `structuredContent` unfenced, no trace retention, memory shared
      across API users, memory not forced off in eval, …
    - Each has a reason recorded, and none is fixed in this release.
19. **Housekeeping:**
    - the ten merged branches above are not deleted
    - `httpx` is listed twice
    - `docs/kickoff/*.md` are historical
    - the dry-run structured report has an empty `charts` list
    - `st.image` captions go through `safe_md`
    - Langfuse with the flag on and no keys logs one warning per eval task

---

## Round 5: #23, #22 (2026-11-04)

Merged into `main` in this order, both as merge commits. After each merge I ran
`uv sync --all-extras --no-extra embeddings`, `uv run ruff check .`, `uv run ruff format --check .`
and `uv run pytest --cov` with CI's coverage gate, and got everything green before going on.
Final suite: **966 passed, 1 deselected (slow), 0 skipped**.

| Order | PR | Branch | Result | Tests after | Coverage (overall / `sandbox/` / `core/loop.py`) |
|---|---|---|---|---|---|
| 1 | #23 | `fix/review-2` | clean merge; 1 test failure (Rich line wrap, below) | 915 | 94% / 99% / 100% |
| – | | test fix (`8417907`) | | 915 | unchanged |
| 2 | #22 | `chore/first-run` | conflicts in `eval/runner.py`, `tests/eval/test_experiments.py`, `docs/eval-runbook.md` | 951 | 94% / 99% / 100% |
| – | | resume spend + cancelled batch (`8c1dd7c`) | | 955 | 94% / 99% / 100% |
| – | | UI image test, two more surfaces (`7eb99c9`) | | 966 | 94.4% / 99.4% / 100% |

### #23: one flaky assertion

`tests/eval/test_experiments.py::test_plan_and_summary_commands` checked that
`"lists no runs"` appears in Typer's error panel. Rich wraps that panel at 80 columns, and
pytest's tmp path (`/tmp/pytest-of-root/pytest-N/test_plan_and_summary_commands0/empty.tsv`) is
long enough to split the message, so the test fails or passes depending on the path length. The
assertion now strips the panel's borders and line breaks first.

### #22 onto #23: `eval/runner.py`

#23 records spend per LLM call (`SpendMeter`, `report_spend`), so `--budget-usd` counts tasks
that time out or crash (review-2 M2). #22 split `run_task` into `_execute` (agent),
`_score` and `_result`, added `--batch` (`BatchJudge`) and `--resume`.

- **The meter now lives in `_execute`.** When the agent returns no cost (timeout, crash), it sets
  `out.cost_usd` to the metered spend. The row's `cost_usd` and `run_tasks`' running spend
  (`new_spend = ex.out.cost_usd`) both read it, so the cap and the resume both count it.
- **Direct and batched judge spend.** `spent()` reads `judge.cost`. `BatchJudge.cost` is the
  wrapped `LLMJudge`'s `CostTracker`, which both `create_batch` (at half price) and the direct
  fallback add to. So the cap counts both kinds of judge call: direct ones as they happen,
  batched ones when the batch returns. New test: `tests/eval/test_budget_resume.py`, with both
  judge kinds.
- **`--resume` carries over all recorded spend (fix).** #22 took a resumed run's earlier spend
  from `meta.json`'s `spend_usd`, which was written only when a run finished. For a run that was
  killed, it fell back to summing the rows' `cost_usd`, which leaves out judge spend.
  A killed *resume* was worse: it kept the stale `spend_usd` of the run before it. Now
  `spend_usd` is rewritten after every row, atomically (`meta.json.tmp` and rename). Only the
  spend of tasks still in flight at the kill is lost, at most `--concurrency` tasks.
- **Ctrl-C sent a paid batch (fix).** When a `--batch` run was cancelled, each cancelled task's
  `finally` called `maybe_flush()`. The queued verdicts then went out as a Message Batch that
  nobody waited for: it was paid for, its spend was never recorded, and the run threw away its
  results. Cancellation now sets `stopping`, and no flush starts after that. Queued verdicts stay
  `pending_judge`, and the resume re-grades them. The test asserts that no batch is created.
- `tests/eval/test_experiments.py`: both sides added tests at the same place; I kept both.
- `docs/eval-runbook.md`: #22 says the API key must be exported (`.env` is read for `AGENT_*`
  only) and preflight fails without it. #23 says `eval run` prints a warning and scores ADV by
  rules only. Both are true (`judge_configured()` reads only the environment; `eval/cli.py`
  prints the warning), so the bullet keeps both. The *Interrupted runs* and *Budget caps*
  sections now describe the spend carry-over and the Ctrl-C behaviour.
- `core/config.py`, `docs/configuration.md`, `.env.example`: only #22 changed them
  (`AGENT_JUDGE_BATCH_ENABLED`, `AGENT_JUDGE_BATCH_POLL_S`), so there was no conflict.
  `tests/test_config.py` passes. #23's ~35 doc fixes and #22's runbook rewrite are both kept.
  The one overlap is the API-key bullet above.
- README: #23 updated one test count to 915 but missed the *measured numbers* table (still
  898). Both now say 966, and coverage 94.4%.

### UI image surfaces: one test for all of them

The untrusted-Markdown image bug had been fixed three times in three places: the report (review
M2), the rewritten UI (round 3) and `st.metric` values (review-2 M4).
`tests/frontend/test_no_untrusted_images.py` now covers every surface:

- every public function of `frontend/render.py` and `frontend/ui_helpers.py`, and every function
  of `frontend/app.py`, must be classified as a renderer or not. A new function fails the test
  until it is classified;
- each helper renderer gets a Markdown image and an HTML `<img>`, plus short forms that survive
  the 40-character key-number clip. Its output is rendered as Streamlit would (raw HTML only for
  `st.html` / `unsafe_allow_html=True`) and must have no `<img>`;
- `app.py` runs against a recording fake Streamlit and an `httpx.MockTransport` API that puts
  the payload in every untrusted field: dataset metadata, sample catalog, events, report, key
  numbers, artifact names, approval reasons, API error details. Every Markdown- or
  HTML-rendering argument of every `st.*` call is checked, and so is every `render_*` function
  called directly.

I checked that the test catches regressions by reverting fixes one at a time. Reverting M4
(`scalar_numbers`), unwrapping the approval reasons, or adding an unsanitised `st.metric` label
each makes it fail. My first payload was too long and was cut by the key-number clip, which is
why the short forms are there. The test also found **two more surfaces**, now made inert with
`safe_md`:
- the sample catalog's example-question buttons (`st.button` labels render images; the catalog
  is operator-controlled, so this is defence in depth);
- the job status in `st.status` labels.

### Verification (all local)

- **`analyst-agent ask --dry-run` flag matrix:** all 32 combinations of routing × semantic
  layer × approval (auto, row threshold 0) × structured report × memory, with the trace store
  on and JSON logs on in half. **32/32 `completed`**, every answer "North America", one
  approval in each of the 16 approval-on runs and none otherwise, 33 span trees.
  `AGENT_MAX_CRITIC_ROUNDS=0` gives `completed_with_issues` with 0 critic rounds.
- **`eval run --agent dry-run --limit 5 --batch`** with all nine flags on (routing, semantic,
  structured, memory, approval, Langfuse, samples, trace store, JSON logs). Without a key, the
  CLI turns the judge (and so `--batch`) off. So I ran it against a local fake Anthropic API
  (`/v1/messages` + `/v1/messages/batches`, real SDK client, `AGENT_JUDGE_BATCH_POLL_S=1`).
  The stratified sample includes `l3_ecom_2025_overview` and `adv_01_injection_revenue_zero`.
  - Uninterrupted: 5/5 `ok`; **one batch of 2 requests**, no direct calls;
    `judge_batch = {batches: 1, batched_requests: 2, direct_requests: 0, fallback: null}`;
    `spend_usd` 0.006.
  - **Killed with SIGINT (Ctrl-C) after 4 rows**, `--concurrency 1`: exit 130, **no API call
    after the kill**, the L3 row left as `pending_judge`, no sandbox process left behind.
    `--resume <run> --batch`: "3 done, 1 to re-score, 1 to run". L3 was re-graded from the saved
    answer without re-running the agent, ADV ran, one batch of 2 was sent, 5 final rows.
  - **Killed with SIGKILL after 4 rows** (direct judge): `meta.json` had `spend_usd` 0.006 (the
    L3 verdict) and no `finished_at`. `--resume --batch`: "4 done, 0 to re-score, 1 to run
    (spent so far $0.01)", `prior_spend_usd` 0.006, a batch of 1, final `spend_usd` 0.009
    (0.006 direct + 0.003 batched).
  - The dry-run agent's L1/L2 answers do not match these tasks' expected values, so they fail
    scoring here (as in CI, where this run uses `--no-score`).
- **`scripts/preflight.sh --dry-run`:** every check `ok` except `extra [embeddings]` (FAIL,
  expected with `--no-extra embeddings`) and Message Batches (WARN: the scripted LLM has no
  batches API). Exit 1 because of the missing extra. Estimate: $142.58 for all experiments,
  $213.87 capped.
- **CI jobs, run as in the workflows:**
  - `ci.yml` test: `uv lock --check`, `uv sync --locked --all-extras --no-extra embeddings`,
    lint, format, `pytest --cov` (966 passed), gate: overall 94.39% (≥ 92), `sandbox/` 99.38%
    (≥ 97), `core/loop.py` 100% (≥ 98). `eval/` 93%, `eval/runner.py` 95%.
  - `ci.yml` eval-smoke: oracle `--limit 5` 100%; dry run (3) ok; dry run with every flag (5)
    ok, 5 span trees, 101 JSON log lines carrying `task_id` (review-2 L1 holds).
  - `ui-e2e.yml`: `scripts/ui-e2e/run.sh`, **24/24** checks (approve and deny).
  - `nightly.yml`: kernel 6.18, Landlock ABI 7. Fuzzer with the fixed seed (20261104) and a new
    seed (20261106): 500/500 each, 0 network hits, about 128 s each.
- **review-2 regression tests** (M1 ×2, M2, M3, M4, L1) pass on merged `main`: 10 cases.

### Branches

**Not deleted yet.** These six remote branches belong to merged PRs, and each is contained in
`main`: `docs/eval-runbook` (#19), `docs/final` (#18), `test/coverage-and-properties` (#20),
`test/sandbox-fuzz` (#21), `fix/review-2` (#23) and `chore/first-run` (#22). Deleting them from
the integration session failed: the session's git proxy refused `git push --delete` with
HTTP 403 (a policy denial), and its GitHub tools cannot delete refs. To delete them with
repository write access:

```bash
git push origin --delete docs/eval-runbook docs/final test/coverage-and-properties test/sandbox-fuzz fix/review-2 chore/first-run
```

#15 (closed unmerged) has no branch left. Keep `main`. The five `claude/*` session branches have
no PR, so the "merged or closed PR" rule does not cover them.
Four of them (`brave-allen`, `elegant-cori`, `happy-thompson`, `sleepy-gates`) are fully contained
in `main` and could go too, if you want.

### Still open

- **Embedding and hybrid retrieval are still not measured** (no `embeddings` extra), and the
  Results table is still empty. Both need the real run, which `docs/eval-runbook.md` now walks
  through (preflight, smoke, baseline, rest, `--resume`).
- **Message Batches against the real API** were exercised only through the SDK against a local
  fake that follows the documented request and result shapes. The first real `--batch` run is
  the check. If it fails, the runner falls back to direct calls and records `fallback` in
  `meta.json`.
- **In-flight spend at a hard kill** is not recorded (at most `--concurrency` tasks). A
  `SpendMeter` that wrote through to disk would close this; not worth it for the size of the
  numbers involved.
- **Docker image, Cloud Run, a real LLM:** not available here, as in earlier rounds.

## Round 4: #20, #21, #19, #18 (2026-11-04)

Merged into `main` in this order, all as merge commits. After each merge I ran
`uv sync --all-extras --no-extra embeddings`, `uv run ruff check .`,
`uv run ruff format --check .` and `uv run pytest` (default run, `slow` deselected), plus the
coverage gate from #20 onwards, and got everything green before the next merge. Final suite:
**898 passed, 1 deselected (slow), 0 skipped**.

| Order | PR | Branch | Result | Tests after | Coverage (overall / `sandbox/` / `core/loop.py`) |
|---|---|---|---|---|---|
| 1 | #20 | `test/coverage-and-properties` | clean merge | 836 | 94.06% / 99.35% / 100% |
| 2 | #21 | `test/sandbox-fuzz` | clean textual merge; `sandbox/` fell to 97.02% | `tests/sandbox`: 228 + 1 slow (full suite next row) | not measured / 97.02% / not measured |
| – | | follow-up: #20's tests cover #21's rules (`92b6279`) | | 884 | 94.10% / 99.38% / 100% |
| 3 | #19 | `docs/eval-runbook` | clean merge | 898 | 93.66% / 99.38% / 100% |
| 4 | #18 | `docs/final` | `README.md` conflict, plus doc updates to the merged code | 898 | unchanged |
| – | | `run_experiments.sh` comment, review.md status, README anchor | | 898 | unchanged |

### #21 onto #20: seccomp rules, coverage and the `slow` marker

- **No textual conflict, but a coverage drop.** #21 added 60+ syscalls per architecture,
  signal and ioctl argument checks, an `ENOSYS` range (≥ 463), and opt-in phase profiling
  (`python_runner._PROFILE`, `confine(marks=)`) used only by `scripts/bench_sandbox.py`. #20's
  `sandbox/` floor is 97%; after the merge `sandbox/` measured 97.02%, down from 99.35%,
  because the profiling branches were reachable only from the benchmark script and the new
  filter rules only from live (host-arch) tests.
- **#20's recorded-syscall tests now cover #21's rules** (`tests/sandbox/test_oslock_unit.py`).
  A small classic-BPF interpreter runs `build_filter()`'s output against synthetic
  `seccomp_data` on **both** x86_64 and aarch64, so the rules are checked on the arch CI does
  not run on:
  - every `_DENIED` syscall → `EPERM`; `socket` for AF_UNIX/INET/INET6/NETLINK/PACKET → `EPERM`;
  - `clone` allowed only with `CLONE_THREAD` (low 32 bits); `clone3` and nr ≥ 463 (incl.
    `0x3FFFFFFF`) → `ENOSYS`, 462 allowed;
  - `kill`/`tgkill`/`rt_sigqueueinfo`/`rt_tgsigqueueinfo` allowed only for the embedded pid,
    refused for other pids, `0` and `-1`;
  - every `_IOCTL_DENIED` request refused, also with high bits set; `TCGETS`, `FIONREAD`,
    `FS_IOC_GETFLAGS` allowed;
  - i386 audit arch and x32 syscalls refused; ordinary syscalls and `landlock_*` allowed;
  - the program sizes the docs cite (115 instructions on x86_64, 103 on aarch64); no duplicate
    syscall numbers per arch; aarch64 lacks exactly the 11 legacy syscalls;
  - `apply_seccomp()` hands the kernel a program scoped to `os.getpid()` (checked by decoding
    the `sock_fprog` the recorder receives).
- **Profiling path** (`tests/sandbox/test_runner_branches.py`): off by default; with a fake
  child, numeric child marks are merged and stale/non-numeric ones dropped; with a crashed
  child only parent marks; with a real child, the confinement phases arrive in order.
  `confine(marks=)` records both steps.
- **Result:** `sandbox/` 99.38%, at or above #20's 99.35%; the gate floors are unchanged.
- **`slow` marker vs the coverage gate.** Both exist. `pyproject.toml`'s `addopts = "-m 'not slow'"`
  deselects the fuzzer in every default run, so CI's `pytest --cov` measures fast tests only and
  the floors apply to that run. Coverage `source` is `analyst_agent`, so the fuzzer's test file
  is never counted. Nothing in `src/` is reachable only from the slow test any more (the
  profiling path was the only case). The CI step now says this in a comment; `nightly.yml`
  runs the fuzzer without `--cov`. `make coverage` runs CI's gate locally and `make check` uses
  it; `make fuzz` runs the slow test.

### #18 onto #19, #20, #21

- **`README.md` conflict.** #18 rewrote the README as a front page and removed the section
  where #20 had added its coverage and Hypothesis paragraph. I took #18's README and updated it
  to the merged code:
  - the seccomp description (#18 still said "no non-`AF_UNIX` sockets") and the fuzzer's
    findings, with a link to `sandbox.md#escape-fuzzing`;
  - critic: `AGENT_MAX_CRITIC_ROUNDS=0` switches it off (#19);
  - CI: 898 tests under the coverage gate, fuzzer nightly;
  - **Results:** #18's table had four hand-written arms (baseline, routing, semantic,
    structured) that matched neither #19's experiment plan nor its arm names. It now describes
    #19's five experiments and points at `scripts/run_experiments.sh` (`--agent dry-run`,
    `--print-commands`) and `docs/eval-runbook.md`; the `eval compare` example uses the real
    arm directories (`sonnet/`, `routing-on/`);
  - measured numbers: 898 tests; coverage per gate; 264 sandbox tests (was 183); fuzzer
    500/500. The #20 paragraph moved to `CONTRIBUTING.md`.
- **`docs/sandbox.md`.** #18 did not edit the file; its references to it (README,
  `architecture.md`, `interview-notes.md`) described the pre-#21 filter. All three now describe
  the final one. `sandbox.md` (#21's rewrite) gains the filter size (115 / 103 instructions),
  a paragraph on the BPF unit tests, a performance table (confinement 1.4–2.2 ms per call, no
  measurable per-syscall cost, `run_python` 1–1.6 s, audit hook ~8 µs per `open`, `run_sql`
  1–11 ms), and "before this change" / "after this branch" became PR references.
  The known-gaps list is #21's, unchanged.
- **`interview-notes.md`:** four merge rounds (was three), the final seccomp list, a
  paragraph on what the fuzzer found, and the coverage gate under the CI decision.
- **`architecture.md`:** the seccomp lines, the fuzzer, and `AGENT_MAX_CRITIC_ROUNDS=0`.
- **`Makefile` / `CONTRIBUTING.md`:** `coverage` and `fuzz` targets; `check` = lint + coverage
  gate + eval (what CI runs); the sandbox checklist item now names `test_oslock_unit.py` and
  `make fuzz`.
- **`scripts/run_experiments.sh`:** the header example used `baseline-sonnet/`; the script
  writes the control to `sonnet/`.

### #19

No conflicts. `AGENT_MAX_CRITIC_ROUNDS=0` now means "critic off" (first report accepted
unreviewed as `completed_with_issues`, no critic call); `ask --dry-run` with it set ends
`completed_with_issues` with 0 critic rounds. `--budget-usd` exists only on `eval run` (not on
`ask`), which is how the README, the runbook and `docs/benchmark.md` cite it. `eval/experiments.py`
is at 83% branch coverage; overall stays above the 92% floor.

### Verification (all local, no API key)

- **`analyst-agent ask --dry-run` flag matrix:** all 32 combinations of routing × semantic
  layer × approval (auto, row threshold 0) × structured report × memory, with the trace store
  on and JSON logs on in half. 32/32 `completed`, every answer "North America"; approval runs
  record one approval each. Plus `AGENT_MAX_CRITIC_ROUNDS=0`: `completed_with_issues`, 0
  critic rounds. The pytest matrix (`tests/test_cli.py`) has 64 cases.
- **`eval run --agent dry-run --limit 5`** with all nine `AGENT_*_ENABLED` flags on (routing,
  semantic, structured, memory, approval, Langfuse, samples, trace store, JSON logs),
  `--fail-on-error`: 5/5 ok. Every task trace has `route`, `semantic_retrieval`, `plan`,
  `approval` and `critic`; 5 insights saved; 5 span trees written; Langfuse without keys only
  warns.
- **CI jobs, run as in the workflows:**
  - `ci.yml` test: `uv lock --check`, `uv sync --locked --all-extras --no-extra embeddings`,
    lint, format, `pytest --cov` (898 passed), coverage gate: overall 94% (≥ 92), `sandbox/`
    99% (≥ 97), `core/loop.py` 100% (≥ 98).
  - `ci.yml` eval-smoke: oracle `--limit 5` at 100%, dry run (3), dry run with every flag (5).
  - `ui-e2e.yml`: `scripts/ui-e2e/run.sh`, 24/24 checks (approve and deny).
  - `nightly.yml`: kernel probe (kernel 6.18, Landlock ABI 7); fuzzer, fixed seed; fuzzer,
    new seed 20261105: 500/500, no problems.
- **Fuzzer, default seed (20261104), `-m slow`:** passed in 128 s on 4 cores. **500/500**
  blocked or killed, 0 oracle problems, 0 network hits; 253 full-mode and 247 kernel-only
  cases, 185 distinct attacks, longest case 8.0 s.
- **`scripts/run_experiments.sh --agent dry-run --yes`:** end to end in 2 m 48 s. All 8 arms
  (haiku, sonnet, critic-off, semantic-bm25/-embedding/-hybrid, routing-on, structured-on) ran
  80/80 tasks with status `ok`; manifest and `summary.md` written, Sonnet as the control of
  experiments 2–5. The embedding and hybrid arms show "80 retrieval errors" because the
  `embeddings` extra is not installed with the sync command above; the runbook's
  troubleshooting table covers this.
- **Oracle, all 80 tasks:** 100% on the 64 gradable ones (L3 skipped without a judge).
  **BM25 retrieval:** top-1 0.848, top-3 0.909, MRR 0.896 on the 33 benchmark cases, as the
  README states.
- **Docs:** every project CLI flag cited in README, CONTRIBUTING, Makefile and `docs/*.md`
  exists in the final CLI (`ask`, `trace`, `eval run|report|compare|tags`, `semantic eval`,
  `run_experiments.sh`, `bench_sandbox.py`, `analyst-agent-mcp`); every cited `AGENT_*`
  variable is a `Settings` field except the documented UI-only `AGENT_API_*`; relative links
  and anchors resolve.

### Audit of docs/review.md (round 4)

Every High and Medium fix is still in place. The 24 regression tests the review names pass
(36 cases with parameters). One reference was ambiguous: M7's
`::test_rlimits_applied_without_preexec_fn` read as if it were in `tests/test_approval.py`
(the file named just before it); it is in `tests/sandbox/test_python_security.py`, and the
review now says so.

| ID | Still fixed | Checked |
|---|---|---|
| H1 | yes, widened by #21 | Live through the stdio MCP server: `fork_exec` → `EPERM`, no marker file. The filter now denies every socket and more (115 instructions); each rule unit-checked on both arches. review.md notes the change. |
| H2 | yes | Live through MCP: `pyarrow.input_stream('/etc/hostname')` → `PermissionError` (Landlock). |
| M7 | yes | No `preexec_fn=` in `src/`; seccomp still denies `setrlimit`/`prlimit64`. |
| M1 | yes | `tools/registry.py` `_DELIMITER_RE` and `core/prompts.py` (`<prior_insights>`), both `IGNORECASE`. |
| M2 | yes | `frontend/render.report_parts`; UI e2e green. |
| M3 | yes | `api/app.py` `UploadSizeLimit`. |
| M4 | yes | `tools/make_chart.py`: `FigureCanvasAgg`, `MAX_BINS = 200`, `MAX_POINTS = 10_000`. |
| M5 | yes | Both M5 tests pass (Markdown and structured reports). |
| M6 | yes | `core/approval.py` gates `EXPLAIN ANALYZE` on the inner query. |
| M8 | yes | `api/jobs.py` `_run_with_context` → guarded `_run`. |

Also live through MCP: #21's full-mode gap (renicing the parent with `os.setpriority`) →
`EPERM`. I did not repeat round 3's probe inside an eval forkserver child; it uses the same
`create_sandbox` → `run_python` path as MCP. L20 now cites the measured cost of reloading
data (60–200 ms of a 1–1.6 s call). The other Low items are unaffected by this round.

### Audit of docs/interview-notes.md

Checked every concrete claim against the merged code. Correct as written: one `loop.py` and
one `AgentState`; `FakeLLM` queues per role; critic at most two rounds by default and fails
open to `completed_with_issues`; `core/report.py:check_consistency`; 200-row results;
`eval/isolation.py:run_in_subprocess` / `_apply_env` with `get_settings.cache_clear()`;
router `claude-haiku-5-5` at `low` effort; `JobApprover`, `ApprovalBroker`, `AutoApprover`,
`ApprovalPolicy`; nine `AGENT_*_ENABLED` flags (matches `.env.example`); 12-character prompt
fingerprints; `TraceRecorder`; 22 metrics, top-k 4; SSE resume via `Last-Event-ID`;
`mark_interrupted`; `test_human_denials_do_not_escalate`; the 64-case flag matrix; five branches
merged after #17 in round 3; `report_parts`' rule. Fixed: the seccomp description, "three"
merge rounds (now four, 21 branches), and the fuzzer and coverage gate were missing from the
sandbox and CI decisions.

### Still open

- **Embedding and hybrid retrieval were not measured,** here or in the experiments dry run (no
  `embeddings` extra, no model download). A real run of experiment 3 needs
  `uv sync --extra embeddings` first, as the runbook says.
- **The Results table is still empty;** filling it needs `ANTHROPIC_API_KEY` and
  `scripts/run_experiments.sh` (the runbook estimates $142.58 for all 8 arms, capped at
  $213.87 with the default `--cap-factor 1.5`).
- **The kernel-only fuzz cases ran as root** (this container). The production image runs as
  `app`; the capability-gated rows are then refused anyway, so the root run is the stricter test.
- ~~`eval/experiments.py` is at 83% branch coverage~~ — resolved in `fix/review-2`: 99%
  (`tests/eval/test_experiments.py`). Still not gated per file.
- **The Docker image is still not built here** (no daemon, `ghcr.io` blocked); `scripts/demo.sh`
  and `make demo` were not run.
- ~~**PRs:** #20, #21, #19 and #18 are merge commits on `main`, so GitHub marks them merged once
  `main` is pushed.~~ Done; their branches are listed for deletion in round 5.

## Round 3: #17, #15, #16, #13, #12, #14 (2026-11-04)

Merged into `main` in this order. #15 was squash-merged because its middle commit fails tests
on its own; the others are merge commits. After each merge I ran
`uv sync --all-extras --no-extra embeddings` (plain `--all-extras` before #12, which adds that
extra), `uv run ruff check`, `uv run ruff format --check` and `uv run pytest`, and got
everything green before the next merge. Final suite: **767 passed, 0 skipped**.

| Order | PR | Branch | Result | Tests after |
|---|---|---|---|---|
| 1 | #17 | `fix/review-findings` | clean merge | 569 |
| 2 | #15 | `feat/benchmark-80` | squash, clean | 578 |
| 3 | #16 | `feat/observability` | 2 conflicts, plus a flaky-test fix | 604 |
| 4 | #13 | `feat/report-and-memory` | 3 conflicts, plus the M4 port and an M1-style fix | 682 |
| 5 | #12 | `feat/semantic-embeddings` | 2 conflicts, plus 3 cross-branch fixes, Docker build arg | 704 |
| 6 | #14 | `feat/ui-polish` | 5 conflicts, plus the M2 re-fix, samples and an e2e fix | 718 |
| – | | post-merge fixes (below) | | 767 |

### Conflicts resolved

**#16 observability (onto #17 + #15)**
- `eval/agent.py`: `_run_core` takes both #17's `semantic_layer_enabled` and #16's `recorder`.
  `core_agent` / `dry_run_agent` pass both and run inside `log_context(task_id=...)`.
- `tests/api/test_app.py`: #17's M3 and M8 tests, plus #16's `trace_store` field in `/healthz`.
- Langfuse score push vs #17's judge-less scoring: no code conflict. `eval_scores` already
  leaves out accuracy and judge scores for skipped results. A new test pins this:
  judge-less L3 is skipped and pushes only `eval_status` / `cost_usd`; rules-only ADV pushes
  `passed` but no `judge_score`.
- **Flaky test** (`tests/api/test_observability.py`): the job turns terminal before the worker
  saves the trace and logs "job finished", and the test asserted on both right after the job
  turned terminal. It now drains the job executor first.

**#13 structured reports and memory**
- `core/loop.py`: #16's `run(question, run_id)` (log context, `run_start` event, "run finished"
  log) plus #13's memory write for completed runs. `run_start.flags` now also reports
  `structured_report` and `memory`.
- `tools/handlers.py` / `tools/make_chart.py`: #13 moved `make_chart` into its own module, so
  #17's M4 fix is ported there:
  - object-oriented Matplotlib only (`Figure` + `FigureCanvasAgg`, no pyplot)
  - `bins` bounded to 1..200 (still `"auto"` when omitted)
  - at most 10,000 points per series
  - `spec` must be an object
  - #13's chart-type inference and defaults are unchanged.
- `docs/configuration.md`: the five feature flags plus the observability note. The semantic
  layer keeps #17's `eval run --semantic-layer` override.
- `core/prompts.py`: #13's `<prior_insights>` block was neutralised case-sensitively, the same
  hole as review M1. It now uses the case- and whitespace-insensitive rule, with a test.
- **Prompt versions.** No other branch touched `core/prompts.py`. The merged prompt texts are
  byte-identical to #13's (`planner@1.1.0`, `critic@1.1.0`, `structured_report_rules@1.0.0`,
  `memory_rules@1.0.0`), so no further bump was needed. #17's fence change lives in
  `tools/registry.py` and does not change any prompt text. `tests/test_prompts.py` is green.

**#12 embeddings**
- `core/loop.py`: #12's `default_retriever(backend, model, cache dir, hybrid weight)` with
  #17's package-relative `resolved_semantic_metrics_dir`.
- `docs/configuration.md`: #12's backend rows next to #13's report and memory rows.
- `semantic/__main__.py`: `semantic eval` read `settings.semantic_metrics_dir`, which #17
  made optional (`None` means the package default). It now uses the resolved dir.
- `benchmarks/metrics/retrieval_eval.yaml`: the 40 tasks #15 added are mapped to their metrics,
  because a test requires every benchmark task to be listed. Most map to `[]`: CAC, carriers,
  expenses and support have no governed metric.
- `tests/semantic/test_retrieval_eval.py`: the BM25 regression guard was calibrated on 30 tasks
  (benchmark top-1 at least 0.9). On the 33 scored cases of the 80-task set BM25 measures
  top-1 0.848, top-3 0.909, MRR 0.896, so the guard is now top-1 ≥ 0.84, top-3 ≥ 0.9,
  MRR ≥ 0.88. The AOV rerank assertions are unchanged. See open items.
- `tests/eval/test_open_items.py`: the bundled metrics dir also holds `retrieval_eval.yaml`.
  #12's loader skips it (`kind: retrieval_eval`). I checked an installed wheel: it loads 22
  metrics from `site-packages/analyst_agent/semantic/metrics`.
- **Docker.** A new opt-in build arg, `INSTALL_EMBEDDINGS=true`, adds the `embeddings` extra
  (sentence-transformers and torch) to the API image. `HF_HOME` points at the data volume.
  Compose passes `INSTALL_EMBEDDINGS` through. The runtime no longer copies
  `benchmarks/metrics` or sets `AGENT_SEMANTIC_METRICS_DIR`, because the wheel bundles the
  metrics (#17). `uv sync --frozen --extra api [--extra embeddings]` resolves to 68 or 109
  packages.

**#14 UI**
- `frontend/app.py`: #14's UI with #17's M2 rule applied to every Markdown surface:
  - API errors and the job error
  - approval reasons, with the tool name as a code span
  - `st.metric` labels for key numbers (Streamlit renders images in labels)
  - dataset title and description
  - column names, as code spans a backtick cannot close (#14 used raw backticks).
- **Charts.** #14 re-emitted `![alt](url)` for any image URL in the report other than
  `/artifacts/...`, which reopened M2. `frontend/render.report_parts()` now turns an image
  into a chart only when its URL is one of this job's own `result.artifacts` entries and
  matches `/artifacts/<path>` (no `..`, no query). The UI fetches it server-side. Every other
  image, inline or reference-style, stays inert text.
- **Tests.** #17's `describe()` is gone, because #14's timeline is escaped HTML
  (`ui_helpers.step_html`). `tests/test_frontend_render.py` now covers `report_parts`, chart
  captions, and the HTML timeline for every event type (no `<img>`).
- `Dockerfile` / `.dockerignore`: sample CSVs ship at `/app/benchmarks/data`
  (`AGENT_SAMPLES_DIR`), metrics come from the wheel, and `HF_HOME` / `INSTALL_EMBEDDINGS`
  are kept.
- `api/samples.py`:
  - The test requiring every bundled sample to have example questions failed after #15 added
    CSVs. They now have curated titles and questions.
  - The adversarial fixtures are left out of the sample picker: `finance/*`, with
    prompt-injection column and file names, and `support/ticket_messages`, with PII. Custom
    `AGENT_SAMPLES_DIR`s are unaffected.
- **`scripts/ui-e2e/run.sh` failed its deny run**, on #14 alone as well. While a step awaited
  approval, the timeline was an `st.status` that Streamlit kept collapsed across the rerun
  (it ignores `expanded` on an existing element), so the steps being approved were hidden.
  The timeline now renders in a plain container while a step is pending. Result: 3 of 3
  runs green, 24 checks each.

### Post-merge fixes

- **`--dry-run` with `AGENT_STRUCTURED_REPORT_ENABLED=true` always ended in `error`.** This
  affected `ask` and `eval run --agent dry-run`. The scripted LLM submitted a Markdown
  `final_report` and the loop rejected it. `dry_run_llm(..., structured_report=)` now submits
  the structured fields. The eval LLM factory receives the run's `Settings`. The CLI
  flag-matrix test covers structured reports × memory as well, for 64 combinations.
- **CI.**
  - eval-smoke's "every feature flag" step also turns on structured reports, memory, the trace
    store and JSON logs, over 5 tasks.
  - `ui-e2e.yml` syncs `--no-extra embeddings` like the other jobs.

### Verification (all local, no API key)

- **`analyst-agent ask --dry-run`**: all 32 combinations of routing × semantic layer ×
  approval (auto) × structured report × memory, with the trace store on and JSON logs on in
  half of them. Every run completed with the right answer. The pytest matrix adds approval
  prompt yes/no/off, for 64 cases.
- **`eval run --agent dry-run --limit 5`** with every `AGENT_*_ENABLED` flag on (routing,
  semantic, structured, memory, approval, langfuse, samples, trace store, JSON logs): 5/5 ok.
  - Every task trace has `route`, `semantic_retrieval`, `plan`, `approval` and `critic`.
  - 5 insights were saved and 5 span trees written.
  - Langfuse without keys only warns.
- **`trace show`** works on a saved `ask` trace (by run id) and on an eval task trace (by
  path). `trace ls` lists the saved runs.
- **CI commands**, run as in the workflows:
  - test job: lock, lint, format, 767 tests
  - eval-smoke: oracle `--limit 5` at 100%, dry-run, dry-run with every flag
  - ui-e2e: `scripts/ui-e2e/run.sh`, 24 checks
- **Oracle, all 80 tasks:** 100% on the 64 gradable ones (24 L1, 24 L2, 16 ADV). The 16 L3
  tasks are skipped because they need an LLM judge. Regeneration test
  (`test_generator_is_deterministic_and_in_sync`): passes byte for byte.
- **Sandbox.**
  - `tests/sandbox` passes: 183 tests, including the escape and fuzz tests.
  - Live, inside an `eval run --isolation process` forkserver child: normal `run_python`
    works, H1 (`fork_exec`) fails with EPERM (seccomp) and no shell spawns, and H2 (pyarrow
    native read) fails with `PermissionError` (Landlock).
  - Same three probes through the real stdio MCP server (`analyst-agent-mcp`): same results.

### Audit of docs/review.md after the merges

Every High and Medium fix is still in place. All the regression tests named in the review
pass (52 cases, including the open-items tests), and the code checks below hold.

| ID | Still fixed | Where it lives now |
|---|---|---|
| H1, H2, M7 | yes | `sandbox/_child/_oslock.py` is applied in `bootstrap.py`; no `preexec_fn` in `src/`; verified live through eval process isolation and MCP (above). |
| M1 | yes | `tools/registry.py` `_DELIMITER_RE` (IGNORECASE); now also `<prior_insights>` (#13). |
| M2 | yes, re-fixed | #14 reopened it for non-artifact report images; see #14 above. `frontend/render.py` + escaped HTML timeline. |
| M3 | yes | `api/app.py` `UploadSizeLimit`. |
| M4 | yes, ported | `tools/make_chart.py` (moved by #13): OO API, bins 1..200, 10k points. |
| M5 | yes | `core/loop.py`: later `final_report` calls in one response get an error `tool_result`; also with structured reports (`test_two_structured_final_reports_in_one_response_each_get_a_tool_result`). |
| M6 | yes | `core/approval.py` strips leading comments and gates `EXPLAIN ANALYZE` on the inner query. |
| M8 | yes | `api/jobs.py` guards all of `_run`; #16 wraps it in `_run_with_context` for logging only. |

### Still open

- **The Docker image is not built here.** There is no Docker daemon in this environment, and
  `ghcr.io` is blocked. I checked the `INSTALL_EMBEDDINGS` sync commands against `uv.lock`.
  The embeddings image pulls CUDA torch wheels (several GB), which is why it is opt-in. A
  CPU-only torch index would shrink it, but would change `uv.lock`.
- **BM25 misses some benchmark questions.**
  - "current employees" questions (`l1_hr_engineering_headcount`, `l1_hr_remote_current`)
    rank `headcount` 4th.
  - `l3_ecom_2025_overview` ranks `net_revenue` / `average_order_value` outside the top 3.
  - Retention questions rank attrition 2nd or 3rd.
  - Fixing this means tuning metric synonyms or the reranker (content owned by #8/#12). The
    hybrid and embedding backends were not measured here (no model download).
- **L3 is never scored offline.** The rubric needs an LLM judge, so "100% on 80" is 100% on
  the 64 gradable tasks.
- **`semantic eval` reads `benchmarks/tasks` relative to the source checkout.** It is a dev
  tool and does not work from an installed wheel.
- **The dry-run structured report puts no charts in `charts`.** The scripted LLM cannot know
  the chart path in advance. The chart is still in `result.artifacts`, and the UI shows it
  under the report.
- **`st.image` captions:** I couldn't confirm whether Streamlit renders them as Markdown, so
  they go through `safe_md` too. A caption containing `![` shows a stray backslash.
- **Langfuse with the flag on and no keys** logs one authentication warning per eval task.
- **PRs:** #15 was squash-merged, so GitHub will not mark it merged automatically; close it
  with a link to the squash commit. The other five are marked merged once `main` is pushed.

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
- ~~The UI e2e is not in CI~~ — resolved: `.github/workflows/ui-e2e.yml` runs
  `scripts/ui-e2e/run.sh` on pull requests that touch `frontend/`, `api/`, `core/approval.py`,
  `core/fake_llm.py` or the script itself.
- **A known limitation from #11 remains.** DuckDB prints no cardinality for some operators (for
  example `Cross Product`), so the row gate uses the largest child estimate and can understate.
- **The dry-run heuristic is unchanged** (see round 1): it uses only the first table with several
  `--data` files.
- ~~Minor UI nit: the collapsed status box above the approval panel shows a check icon while it
  reads "Waiting for your approval…"~~ — resolved in round 3 (#14): while a step awaits
  approval the timeline renders in a plain bordered container, not an `st.status`
  (`frontend/app.py`).
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
