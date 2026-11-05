# Eval runbook: five experiments on the full benchmark

How to run the five headline experiments with real models on all 80 benchmark tasks
([benchmark.md](benchmark.md)), with the LLM judge grading L3 (and ADV behaviour), each run
capped in USD, and how to turn the results into README tables.

| # | Experiment | Arms | Question it answers |
|---|---|---|---|
| 1 | **baseline** | Haiku vs Sonnet, every feature off | How much does the working model matter? |
| 2 | **critic** | critic on (control) vs off | Does the critic step earn its cost? |
| 3 | **semantic** | off (control) vs `bm25` / `embedding` / `hybrid` | Do governed metric definitions help, and which retriever? |
| 4 | **routing** | off (control) vs on (L1 → Haiku, L2/L3 → Sonnet) | Can routing cut cost without losing accuracy? |
| 5 | **structured** | off (control) vs on | Does the structured `final_report` schema change accuracy or cost? |

The Sonnet arm of experiment 1 is the **shared control** of experiments 2-5: it runs once, and
each treatment changes exactly one thing relative to it. That is 8 runs of 80 tasks.

## TL;DR

```bash
uv sync --locked --all-extras
```

```bash
scripts/preflight.sh
```

```bash
scripts/run_experiments.sh --limit 8 --only baseline --batch --yes --out runs/experiments/smoke
```

```bash
scripts/run_experiments.sh --only baseline --batch --out runs/experiments/baseline
```

```bash
scripts/run_experiments.sh --only critic --only semantic --only routing --only structured --batch --control "$(ls -d runs/experiments/baseline/sonnet/2*/ | tail -n 1)" --out runs/experiments/rest
```

1. Install everything, including the `embeddings` extra the embedding/hybrid arms need.
2. Pre-flight: key, models, extras, benchmark, metrics, output dirs, cost per experiment. A few
   1-token API calls (well under $0.01); exits 1 on any failure.
3. Smoke test: 8 stratified tasks × 2 arms, about $2.31 estimated (cap $3.47).
4. Baseline on all 80 tasks: about $21.81 (cap $32.71). Its Sonnet arm is the control of the rest.
5. The other four experiments, reusing that control: about $118.47 (cap $177.70).

Total about **$140** expected with `--batch` ($143 without); the caps stop it at about
**$210** (plus at most `--concurrency` in-flight tasks per arm; see
[Cost estimates](#cost-estimates)). Each step asks before spending; `--yes` skips the prompt.
[Tomorrow's run](#tomorrows-run-step-by-step) has what to check after each step, and
[Results template](#results-template) how the numbers get into the README.

## Tomorrow's run, step by step

Run everything from the repo root, in one shell, in this order. Do not start a step until the
previous one's checks pass. Every `run_experiments.sh` invocation below writes to a fixed
`--out` directory, so a step that is interrupted can be continued with `--resume` (see
[Interrupted runs](#interrupted-runs-resume)).

### 0. Setup (free)

```bash
uv sync --locked --all-extras
```

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

```bash
git status --short && git log -1 --oneline
```

```bash
uv run python -m analyst_agent.semantic eval --backend bm25 --backend embedding --backend hybrid
```

The key must be **exported**: `.env` only feeds `AGENT_*` settings, not the SDK. Commit or
stash local changes so every run's `git_sha` describes the code. The last command downloads
and warms the embedding model and prints retrieval quality per backend (no API calls).

### 1. Pre-flight (< $0.01)

```bash
scripts/preflight.sh
```

```bash
scripts/preflight.sh --limit 8 --batch --skip-regenerate
```

Check:

- The last line says `pre-flight passed` and the exit status is 0. A `WARN` is fine (an
  unclean git tree, or *Message Batches API* unavailable, in which case `--batch` falls back to
  direct judge calls at full price).
- `API key valid` and one `model …` line per model (`claude-haiku-5-5`, `claude-sonnet-5-5`)
  are `ok`, and `judge structured output` is `ok`: the calls went through `core/llm.py` with the
  same effort parameter and refusal-fallback beta as the real run.
- The cost table matches [Cost estimates](#cost-estimates) (the second command prints the
  smoke-run numbers with the batch-priced judge).

Rehearse without a key or network with `scripts/preflight.sh --dry-run` (the API checks go to
the scripted FakeLLM).

Then check the L3 judge against the hand-labelled calibration set (72 judge calls; rehearse
offline with `--fake`):

```bash
uv run python -m analyst_agent.eval judge-calibrate --judge-samples 3 --out runs/judge-calibration.md
```

Read the verdict agreement and kappa, and every listed disagreement, before trusting L3 numbers.
A `subtly_wrong` sample the judge passes is the failure that matters. See
[benchmark.md](benchmark.md#judge-prompt-v2-rubric_judge200) and
`benchmarks/judge_calibration/README.md`.

### 2. Smoke test: 8 tasks, baseline only (~$2.31, cap $3.47)

```bash
scripts/run_experiments.sh --limit 8 --only baseline --batch --yes --out runs/experiments/smoke
```

It takes a few minutes for the agents plus the time the judge batch takes (usually minutes;
`eval run` polls every `AGENT_JUDGE_BATCH_POLL_S` seconds and prints the results once the
verdicts are in). Check:

- Exit status 0 and no `incomplete arms` list.
- Each arm's numbers (also in `runs/experiments/smoke/<arm>/<run>/report.md`):

  ```bash
  grep -hE '^\| (Accuracy \(scored\)|Skipped|Not started|Errors / timeouts|Total cost|Mean cost / task) ' runs/experiments/smoke/*/2*/report.md
  ```

  *Errors / timeouts* 0, *Not started* 0, *Skipped* 0 (a skipped L3 task means the judge did
  not grade it), and Sonnet's accuracy clearly above 0.
- The judge was batched and every verdict came back:

  ```bash
  uv run python -c 'import json,glob; [print(p, json.load(open(p)).get("judge_batch"), json.load(open(p))["spend_usd"]) for p in sorted(glob.glob("runs/experiments/smoke/*/2*/meta.json"))]'
  ```

  `batches` 1, `fallback` null (a non-null fallback is not an error, the judge then ran at full
  price). `spend_usd` should be within about 2× of the estimate; much higher means the token
  profiles are off: raise `--cap-factor` for the next steps.
- Read one L3 verdict to see the judge is grading sensibly:
  `uv run python -m analyst_agent.eval report "$(ls -d runs/experiments/smoke/sonnet/2*/ | tail -n 1)"`.

The smoke numbers are 8 tasks: never put them in the README.

### 3. Baseline on all 80 tasks (~$21.81, cap $32.71)

```bash
scripts/run_experiments.sh --only baseline --batch --out runs/experiments/baseline
```

Check:

- Exit status 0, no `incomplete arms`; in both `report.md`s *Not started* 0 and *Errors /
  timeouts* at most one or two (many L3 timeouts: re-run the arm with `--timeout 900`).
- **Cost calibration.** Compare each arm's `spend_usd` (command above, with `baseline` for
  `smoke`) with its estimate ($1.30 Haiku, $20.51 Sonnet with the batched judge). If Sonnet
  spent more than about 1.3× its estimate, run step 4 with `--cap-factor 2` (or higher: the cap
  must exceed the estimate × the observed ratio), otherwise arms will stop at their caps.
- Remember the control run for step 4:

  ```bash
  CONTROL=$(ls -d runs/experiments/baseline/sonnet/2*/ | tail -n 1); echo "$CONTROL"
  ```

### 4. The other four experiments (~$118.47, cap $177.70)

```bash
scripts/run_experiments.sh --only critic --only semantic --only routing --only structured --batch --control "$CONTROL" --out runs/experiments/rest
```

This is six runs of 80 tasks (critic-off, three semantic arms, routing-on, structured-on) and
takes the longest. Check after it, per experiment (see [Reading the results](#reading-the-results)):

- Exit status 0 and no `incomplete arms`; *Notes* in `runs/summary.md` empty for every row.
- **critic**: critic-off has *Critic rounds* 0.00.
- **semantic**: *Layer on* 100% and *Retrieval errors* 0% in all three arms (errors in
  embedding/hybrid mean the `embeddings` extra or the model download is missing: re-run those
  arms after fixing it).
- **routing**: *Routed tiers* roughly follows the labelled difficulty; cost ratio below 1.00×.
- **structured**: *Numeric text-fallback* lower than the control's.

### 5. One summary over both invocations, then the README

```bash
uv run python -m analyst_agent.eval.experiments summary runs/experiments/baseline/manifest.tsv runs/experiments/rest/manifest.tsv --out runs/summary.md
```

Then paste into the README as described in [Results template](#results-template): replace the
*Not yet measured with a live model* paragraph under `## Results` with the summary's header
line, the *Overview* table and the experiment tables, write the *Findings* bullets by hand,
tick *Publish the Results table* in the roadmap, and commit. `runs/` is git-ignored: keep a
copy (`tar czf runs-$(date +%F).tgz runs/experiments runs/summary.md`) since the README now
quotes it.

## Interrupted runs (`--resume`)

A run interrupted by its `--budget-usd` cap, a crash, a lost connection or Ctrl-C can be
continued without paying again for what already finished:

```bash
scripts/run_experiments.sh --only critic --only semantic --only routing --only structured --batch --control "$CONTROL" --out runs/experiments/rest --resume runs/experiments/rest --cap-factor 2
```

Same options as the interrupted invocation, plus `--resume <its --out>`. Each arm that already
has a run there is continued with `eval run --resume <run_dir>`; arms that never started run
normally. Raise `--cap-factor` (or `--budget-usd`) when an arm stopped at its cap: the cap
covers the whole run, including what was spent before the interruption. That earlier spend is
`meta.json`'s `spend_usd`, which the runner rewrites after every row: agent spend (also the
metered spend of tasks that timed out), and judge calls, direct or batched. A run killed
mid-way therefore resumes with everything it recorded; only the spend of tasks still in flight
at the kill is lost (at most `--concurrency` tasks). A Ctrl-C never sends a batch: verdicts still
queued stay `pending_judge` and are re-graded on resume.

By hand, with the arm's environment exported (`run_experiments.sh --print-commands` prints it):

```bash
uv run python -m analyst_agent.eval run --resume runs/experiments/rest/critic-off/<run> --budget-usd 40 --batch
```

What a resume does with each task of the run (task list, agent, judge model, timeout and flags
come from the run's `meta.json`; task filters are refused):

| Row in `results.jsonl` | Resume |
|---|---|
| scored (`ok`, with a verdict for L3/ADV) | skipped |
| missing (crash) or `over_budget` | run |
| `ok` but no verdict: `pending_judge` (`--batch` interrupted before the batch returned), judge unavailable or failed | re-graded from the saved answer, **without** re-running the agent |
| `error` / `timeout` | kept as the measured outcome; `--retry-errors` re-runs them |

It refuses to start if any `AGENT_*` setting that changes the measurement (models, flags,
limits, judge) differs from the one recorded in `meta.json`. New rows are appended;
`results.jsonl` readers keep the last row per task, and `meta.json` gets a `resumes` entry.

## Batched judge (`--batch`)

`eval run --batch` (or `AGENT_JUDGE_BATCH_ENABLED=true`) queues every judge call of the run
and sends them as one [Message Batch](https://docs.anthropic.com/en/docs/build-with-claude/batch-processing)
once every remaining task is waiting on its verdict, at half the list price. The judge is
independent per task, so nothing else changes; the agent loops are multi-turn and are not
batched. While the batch is pending, L3/ADV rows are written with a `pending_judge` score (so a
crash keeps the paid-for answers for `--resume`), followed by the verdict. `meta.json` records
`judge_batch: {batches, batched_requests, direct_requests, fallback}`.

It falls back to direct calls, silently apart from `fallback` in `meta.json`, when the SDK has
no `messages.batches`, the batch cannot be created, or it has not ended after 6 hours
(cancelled); a single request that errors inside the batch is retried directly. Judge spend
is known only when the batch returns, so `--budget-usd` sees it late (the judge is under 3% of
a run's estimate).

## Before you start

- **API key.** Export `ANTHROPIC_API_KEY` in the shell (`.env` is read for `AGENT_*` settings
  only). Without it `eval run` turns the judge off with only a warning on stderr, **skips every
  L3 task** and scores ADV by rules only; `run_experiments.sh --agent core` refuses to start
  instead, and `scripts/preflight.sh` fails.
- **Clean tree.** Every run records `git_sha` in `meta.json`. Commit first so the summary points
  at the code that produced it.
- **No local flags leak in.** Every arm pins all feature flags explicitly (`AGENT_MEMORY_ENABLED`
  and `AGENT_APPROVAL_ENABLED` are pinned off too); exported variables take precedence over
  `.env`. If you run commands by hand, use the `export` block below.
- **Embedding model.** The embedding/hybrid arms load
  `sentence-transformers/all-MiniLM-L6-v2` (downloaded from Hugging Face on first use, then
  cached). Warm it and sanity-check retrieval quality offline, at no API cost:

  ```bash
  uv run python -m analyst_agent.semantic eval --backend bm25 --backend embedding --backend hybrid
  ```

  If the extra is missing, the semantic layer does **not** fail the run: it records a
  retrieval error and the agent continues without definitions. The summary shows this in the
  *Retrieval errors* column; a real run with errors there is not a valid embedding/hybrid
  measurement.
- **Rate limits.** `--concurrency 4` (default) keeps 4 agent loops in flight. Lower it if you
  see 429 retries piling up (the latency p95 column shows it).

## Fixed settings across all arms

| Setting | Value | Why |
|---|---|---|
| Tasks | all 80 (24 L1, 24 L2, 16 L3, 16 ADV) | no `--limit`, no filters |
| Judge | `claude-sonnet-5-5` (`--judge-model` and `AGENT_JUDGE_MODEL`), effort `low` | one judge for every arm, so judge bias is shared, not confounded with the arm |
| Critic model | `AGENT_CRITIC_MODEL=` (empty = same as the working model) | |
| Effort, `max_tokens`, `max_turns`, token budget | defaults (`high`, 16000, 15, 400k) | |
| Per-task timeout | `--timeout 600` | L3 tasks with Sonnet can exceed the 300 s default |
| Isolation | `process` (default) | a hung task is killed, not leaked |
| Approval gates / memory | off | memory would let earlier tasks leak findings into later ones |

## Budget caps

`eval run --budget-usd X` stops **starting** tasks once the run's agent + judge spend reaches
`X` (costs from `core/llm.py`'s `PRICING`; cache writes 1.25×, cache reads 0.1× input). Tasks
already in flight finish, so a run can overshoot by up to `--concurrency` tasks. Tasks that never
started are written to `results.jsonl` with `status: over_budget`, excluded from accuracy, shown
as *Not started (--budget-usd)* in `eval report`, and flagged in the summary's *Notes* column.
`meta.json` records `spend_usd` and `budget: {usd, exhausted, tasks_not_started}`.
Tasks that time out or crash count too: the core agents report each LLM call's cost as it happens,
and the runner records what a killed task had spent as its `cost_usd` (tokens stay 0). Judge
calls count whether they are direct or batched (`--batch`: when the batch returns, at half
price). On `--resume` the cap starts from the spend the earlier run recorded (see
[Interrupted runs](#interrupted-runs-resume)).

`run_experiments.sh` sets each arm's cap to its estimate × `--cap-factor` (default 1.5) and, by
default, stops starting new arms once the total spend reaches the sum of those caps
(`--max-total-usd` to lower it). An arm that hit its cap makes the script exit 1 and is listed
under *incomplete arms*; its accuracy is over fewer tasks and **should not be compared** until
re-run with a higher cap.

The per-run token budget (`AGENT_TOKEN_BUDGET`, 400k tokens per task) still applies inside each
task.

## Cost estimates

Estimated with `core/llm.py`'s price table (USD per 1M tokens, input/output: Haiku 5.5 0.10/0.50,
Sonnet 5.5 2.00/10.00). The tables below price the judge directly; with `--batch` it costs half
(Message Batches price), which takes $2.30 off the plan. Regenerate after a price or plan change:

```bash
uv run python -m analyst_agent.eval.experiments plan
```

```bash
scripts/preflight.sh --skip-api --skip-regenerate --batch
```

The second prints the per-experiment totals with the batched judge (add `--limit 8` for the
smoke run).

| Experiment | Arm | Agent model | Critic | Judge | Agent $ | Critic $ | Router $ | Judge $ | **Est. total** | Cap (×1.5) |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline | haiku | haiku-5-5 | haiku-5-5 | sonnet-5-5 | $0.83 | $0.19 | $0.00 | $0.58 | **$1.59** | $2.38 |
| baseline | sonnet | sonnet-5-5 | sonnet-5-5 | sonnet-5-5 | $16.51 | $3.71 | $0.00 | $0.58 | **$20.80** | $31.19 |
| critic | critic-off | sonnet-5-5 | off | sonnet-5-5 | $16.51 | $0.00 | $0.00 | $0.58 | **$17.08** | $25.63 |
| semantic | semantic-bm25 | sonnet-5-5 | sonnet-5-5 | sonnet-5-5 | $16.80 | $3.71 | $0.00 | $0.58 | **$21.08** | $31.63 |
| semantic | semantic-embedding | sonnet-5-5 | sonnet-5-5 | sonnet-5-5 | $16.80 | $3.71 | $0.00 | $0.58 | **$21.08** | $31.63 |
| semantic | semantic-hybrid | sonnet-5-5 | sonnet-5-5 | sonnet-5-5 | $16.80 | $3.71 | $0.00 | $0.58 | **$21.08** | $31.63 |
| routing | routing-on | L1/L2/L3 haiku-5-5/sonnet-5-5/sonnet-5-5, router haiku-5-5 | sonnet-5-5 | sonnet-5-5 | $14.02 | $3.71 | $0.02 | $0.58 | **$18.33** | $27.49 |
| structured | structured-on | sonnet-5-5 | sonnet-5-5 | sonnet-5-5 | $17.24 | $3.71 | $0.00 | $0.58 | **$21.53** | $32.30 |
| **all** | | | | | | | | | **$142.58** | **$213.87** |

Per experiment, counting the shared Sonnet control ($20.80) once in experiment 1 and again for
any experiment you run on its own:

| Experiment | Runs | Est. with the control already run | Est. run alone (incl. control) |
|---|---|---|---|
| 1 baseline | haiku + sonnet | $22.39 | $22.39 |
| 2 critic | critic-off | $17.08 | $37.88 |
| 3 semantic | bm25 + embedding + hybrid | $63.25 | $84.05 |
| 4 routing | routing-on | $18.33 | $39.13 |
| 5 structured | structured-on | $21.53 | $42.33 |

**What the estimate assumes.** Per-task token profiles in
`analyst_agent/eval/experiments.py` (planning assumptions, not measurements):

| Level | Agent loop: input / output / cache write / cache read | Critic (≈1.3 reviews) | Judge |
|---|---|---|---|
| L1 | 15k / 4k / 12k / 45k (≈4 turns) | 7k / 1.5k | – |
| L2 | 30k / 9k / 15k / 110k (≈7 turns) | 11k / 2.5k | – |
| L3 | 60k / 20k / 20k / 260k (≈12 turns) | 20k / 4k | 6k / 1k |
| ADV | 20k / 5k / 12k / 60k (≈5 turns) | 9k / 2k | 4k / 0.6k |

Plus: router 1.5k / 0.15k per task (routing arm); semantic layer +1.2k input and +6k cache
read per task; structured report +0.6k input and +0.8k output per task. The routing estimate
assumes the router sends L1 tasks to L1, L2 and ADV to L2, and L3 to L3, with no escalations.
Haiku arms probably use *more* turns than this, and thinking-heavy L3 runs can produce more
output, so treat the numbers as ±50%. After the baseline, check the real mean cost per task
(`Mean cost / task` in each run's `report.md`) and, if it is far off, rerun the remaining
experiments with `--budget-usd` set by hand or a larger `--cap-factor`.

## Running with the script

```bash
scripts/run_experiments.sh --help
```

| Command | What it does |
|---|---|
| `scripts/run_experiments.sh` | all 8 runs, asks for confirmation, writes `runs/experiments/<stamp>/` and `runs/summary.md` |
| `scripts/run_experiments.sh --print-commands` | prints the exact env + `eval run` command of every arm (with its cap) and exits |
| `scripts/run_experiments.sh --only routing --control runs/experiments/<stamp>/sonnet/<run>` | one experiment, reusing an existing Sonnet control |
| `scripts/run_experiments.sh --only semantic --only critic --cap-factor 2` | several experiments, more headroom |
| `scripts/run_experiments.sh --agent dry-run --yes` | the free end-to-end check |
| `scripts/run_experiments.sh ... --batch` | judge calls of each arm as one Message Batch (half price) |
| `scripts/run_experiments.sh ... --out DIR --resume DIR` | continue an interrupted invocation ([Interrupted runs](#interrupted-runs-resume)) |

Layout of one invocation:

```
runs/experiments/<stamp>/
  manifest.tsv              experiment, arm, role (control|treatment), run_dir, config
  summary.md                copy of runs/summary.md
  <arm>.log                 eval run stdout/stderr
  <arm>/<run stamp>/        a normal eval run: meta.json, results.jsonl, report.md
```

The script continues past a failed arm, records it, and exits 1 at the end with the list of
incomplete arms (failed, hit its cap, or not started because of `--max-total-usd`).

To rebuild the summary from one or more manifests (for example after re-running one arm and
editing its `run_dir` in the manifest; with several, a later row replaces an earlier one for the
same experiment and arm):

```bash
uv run python -m analyst_agent.eval.experiments summary runs/experiments/baseline/manifest.tsv runs/experiments/rest/manifest.tsv --out runs/summary.md
```

## Running by hand

The script only wraps the commands below. Run them from the repo root; every `eval run` writes
`runs/experiments/manual/<arm>/<timestamp>/`. First pin the shared settings in your shell:

```bash
export AGENT_MODEL=claude-sonnet-5-5 AGENT_CRITIC_MODEL= AGENT_MAX_CRITIC_ROUNDS=2 AGENT_ROUTING_ENABLED=false AGENT_MODEL_L1= AGENT_MODEL_L2= AGENT_MODEL_L3= AGENT_SEMANTIC_LAYER_ENABLED=false AGENT_SEMANTIC_BACKEND=bm25 AGENT_STRUCTURED_REPORT_ENABLED=false AGENT_MEMORY_ENABLED=false AGENT_APPROVAL_ENABLED=false AGENT_JUDGE_MODEL=claude-sonnet-5-5
```

### 1. Baseline: Haiku vs Sonnet

```bash
AGENT_MODEL=claude-haiku-5-5 uv run python -m analyst_agent.eval run --agent core --judge-model claude-sonnet-5-5 --concurrency 4 --timeout 600 --budget-usd 2.38 --no-routing --no-semantic-layer --runs-dir runs/experiments/manual/haiku
```

```bash
uv run python -m analyst_agent.eval run --agent core --judge-model claude-sonnet-5-5 --concurrency 4 --timeout 600 --budget-usd 31.19 --no-routing --no-semantic-layer --runs-dir runs/experiments/manual/sonnet
```

The second run is the control for experiments 2-5.

### 2. Critic on vs off

`AGENT_MAX_CRITIC_ROUNDS=0` switches the critic off: the first `final_report` is accepted
unreviewed (agent status `completed_with_issues`, no critic call).

```bash
AGENT_MAX_CRITIC_ROUNDS=0 uv run python -m analyst_agent.eval run --agent core --judge-model claude-sonnet-5-5 --concurrency 4 --timeout 600 --budget-usd 25.63 --no-routing --no-semantic-layer --runs-dir runs/experiments/manual/critic-off
```

### 3. Semantic layer: bm25 / embedding / hybrid

```bash
AGENT_SEMANTIC_LAYER_ENABLED=true AGENT_SEMANTIC_BACKEND=bm25 uv run python -m analyst_agent.eval run --agent core --judge-model claude-sonnet-5-5 --concurrency 4 --timeout 600 --budget-usd 31.63 --no-routing --semantic-layer --runs-dir runs/experiments/manual/semantic-bm25
```

```bash
AGENT_SEMANTIC_LAYER_ENABLED=true AGENT_SEMANTIC_BACKEND=embedding uv run python -m analyst_agent.eval run --agent core --judge-model claude-sonnet-5-5 --concurrency 4 --timeout 600 --budget-usd 31.63 --no-routing --semantic-layer --runs-dir runs/experiments/manual/semantic-embedding
```

```bash
AGENT_SEMANTIC_LAYER_ENABLED=true AGENT_SEMANTIC_BACKEND=hybrid uv run python -m analyst_agent.eval run --agent core --judge-model claude-sonnet-5-5 --concurrency 4 --timeout 600 --budget-usd 31.63 --no-routing --semantic-layer --runs-dir runs/experiments/manual/semantic-hybrid
```

`hybrid` uses `AGENT_SEMANTIC_HYBRID_WEIGHT=0.5` (the default).

### 4. Routing on vs off

```bash
AGENT_ROUTING_ENABLED=true AGENT_MODEL_L1=claude-haiku-5-5 AGENT_MODEL_L2=claude-sonnet-5-5 AGENT_MODEL_L3=claude-sonnet-5-5 uv run python -m analyst_agent.eval run --agent core --judge-model claude-sonnet-5-5 --concurrency 4 --timeout 600 --budget-usd 27.49 --routing --no-semantic-layer --runs-dir runs/experiments/manual/routing-on
```

The router is `AGENT_ROUTER_MODEL` (default `claude-haiku-5-5`). Escalation (second critic
`revise` or a repeated tool error) moves an L1 task to Sonnet ([model-routing.md](model-routing.md)).
`eval report` on this run adds a **Routing** section (routed tier vs labelled difficulty,
escalations, cost by model).

### 5. Structured report on vs off

```bash
AGENT_STRUCTURED_REPORT_ENABLED=true uv run python -m analyst_agent.eval run --agent core --judge-model claude-sonnet-5-5 --concurrency 4 --timeout 600 --budget-usd 32.30 --no-routing --no-semantic-layer --runs-dir runs/experiments/manual/structured-on
```

### Comparing by hand

Any two runs, B relative to A, including per-task flips (here the latest Sonnet control vs the
latest critic-off run):

```bash
uv run python -m analyst_agent.eval compare "$(ls -d runs/experiments/manual/sonnet/*/ | tail -n 1)" "$(ls -d runs/experiments/manual/critic-off/*/ | tail -n 1)"
```

All runs at once: write a manifest (tab-separated, header
`experiment	arm	role	run_dir	config`, one row per arm, the Sonnet control listed with role
`control` under each experiment it belongs to) and run the `summary` command above.

## Reading the results

- **Accuracy** is over *scored* tasks. Tasks the judge could not grade and tasks never started
  because of the cap are excluded and called out in *Notes*. Any note other than an empty cell
  means the row is not directly comparable.
- **Noise.** One L1/L2 task is 4.2 pp of its level, one L3/ADV task 6.25 pp, and the judge
  adds variance on L3/ADV. Treat differences of one or two tasks as noise. For a claim you want
  to put in the README, repeat the two arms involved (e.g. `--only critic` twice, reusing the
  same control) and report the range.
- **Cost** is estimated from the price table, not billed amounts. `$/task` is the agent (incl.
  critic and router); *Total $* adds the judge, which is the same model in every arm.
- **Critic experiment.** With the critic off, *Critic rounds* is 0 and every task ends
  `completed_with_issues`. In the control, *Intervention* is the share of tasks with at least
  one `revise`; *Correction* is the share of those that ended up passing.
- **Semantic experiment.** *Layer on* should be 100% in the treatment arms and *Retrieval
  errors* 0%. *Metrics retrieved* is the mean number of definitions injected.
- **Routing experiment.** *Routed tiers* shows how the router split the 80 tasks; compare it
  with the labelled difficulty in that run's `report.md`. A cost ratio well below 1.00× with
  Δ acc within noise is the result routing is for.
- **Structured experiment.** *Numeric text-fallback* is the share of numeric tasks graded from
  the report text rather than `key_numbers`; the structured schema should push it toward 0.

## Results template

`runs/summary.md` has this shape (all numbers below are placeholders). To put it in the README:

1. Build it from the full runs only (step 5 above): the baseline and rest manifests, never the
   smoke run. Every row's *Notes* cell should be empty; a row with a note is not comparable
   and should be re-run (`--resume`) or left out with the reason stated.
2. In `README.md`, under `## Results`, replace the paragraph starting **Not yet measured with a
   live model** with the summary's header line (tasks, judge, commit, date), the *Overview*
   table and the experiment tables you want. Keep the paragraph about the plan and the commands
   below it.
3. Write the *Findings* bullets by hand from the tables (template below). Use *Δ acc* and the
   noise rule in [Reading the results](#reading-the-results): a difference of one or two tasks
   is not a finding.
4. Tick `Publish the Results table` in the README roadmap, commit README + this runbook if the
   estimates changed, and archive `runs/` (it is git-ignored).

```markdown
## Benchmark results

80 tasks (24 L1, 24 L2, 16 L3, 16 ADV), judge claude-sonnet-5-5, commit `<sha>`, <date>.
Accuracy over scored tasks; cost estimated from list prices. One L3/ADV task = 6.25 pp.

| Experiment | Arm | Accuracy | L1 | L2 | L3 | ADV | $/task | Total $ (incl. judge) | p50 latency | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline | haiku | __% | __% | __% | __% | __% | $__ | $__ | __s | |
| baseline | sonnet | __% | __% | __% | __% | __% | $__ | $__ | __s | |
| critic | critic-off | __% | __% | __% | __% | __% | $__ | $__ | __s | |
| semantic | semantic-bm25 | __% | __% | __% | __% | __% | $__ | $__ | __s | |
| semantic | semantic-embedding | __% | __% | __% | __% | __% | $__ | $__ | __s | |
| semantic | semantic-hybrid | __% | __% | __% | __% | __% | $__ | $__ | __s | |
| routing | routing-on | __% | __% | __% | __% | __% | $__ | $__ | __s | |
| structured | structured-on | __% | __% | __% | __% | __% | $__ | $__ | __s | |

### Critic on vs off (Sonnet)

| Arm | Config | Accuracy | Δ acc | L1 | L2 | L3 | ADV | $/task | Cost vs control | p50 / p95 latency | Critic rounds | Intervention | Correction |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| sonnet (control) | Sonnet, all features off | __% | – | __% | __% | __% | __% | $__ | 1.00× | __s / __s | __ | __% | __% |
| critic-off | AGENT_MAX_CRITIC_ROUNDS=0 | __% | ±__ pp | __% | __% | __% | __% | $__ | __× | __s / __s | 0.00 | 0.0% | – |

(one such table per experiment; the semantic table adds Layer on / Metrics retrieved /
Retrieval errors, routing adds Routed tiers / Escalation, structured adds Numeric
text-fallback / Critic rounds)

**Findings**
- Model: Sonnet vs Haiku: __ pp accuracy for __× the cost per task; the gap is largest on __.
- Critic: __ pp (__ tasks); costs __% more per task; intervened on __% of tasks, __% of those
  then passed.
- Semantic layer: best backend __ (__ pp vs off); embedding vs bm25 __.
- Routing: __× cost at __ pp accuracy; the router sent __ of 24 L1 tasks to L1; __% escalated.
- Structured report: __ pp; numeric text-fallback __% → __%.
- Caveats: single run per arm, synthetic data, judge = __ (see docs/benchmark.md).
```

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| *Notes* says "no judge: L3 skipped" | `ANTHROPIC_API_KEY` was not visible to `eval run`; L3 was not graded. Re-run the arm. |
| *Notes* says "N not started (budget)" | The arm hit its `--budget-usd`. Continue it with the same command plus `--resume <its --out> --cap-factor 2` (only the unstarted tasks run; see [Interrupted runs](#interrupted-runs-resume)). |
| Run crashed / Ctrl-C / connection lost | Same: `--resume <its --out>`. Finished tasks are not re-run; answers still waiting for a verdict are re-graded without re-running the agent. |
| `cannot resume: AGENT_* settings differ` | The shell does not have the arm's environment. Export it from `run_experiments.sh --print-commands`, or resume through the script. |
| `meta.json` shows `judge_batch.fallback` | Batching was not used (SDK without `messages.batches`, batch rejected, or > 6 h); the judge ran directly at full price. The scores are valid. |
| `scripts/preflight.sh` fails `extra [embeddings]` | `uv sync --locked --all-extras` (pulls torch). |
| *Retrieval errors* > 0% in embedding/hybrid | `sentence-transformers` missing or the model download failed. `uv sync --extra embeddings`, warm with `python -m analyst_agent.semantic eval -b embedding`. |
| Many timeouts on L3 | Raise `--timeout`, or lower `--concurrency` if 429 retries are stretching tasks. |
| Script exits 1 but `runs/summary.md` exists | Some arm is incomplete; the summary marks it in *Notes*. See the list the script prints and `<arm>.log`. |
