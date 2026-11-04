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
scripts/run_experiments.sh --agent dry-run --yes
```

```bash
scripts/run_experiments.sh --limit 8 --only baseline --yes
```

```bash
scripts/run_experiments.sh
```

1. Install everything, including the `embeddings` extra the embedding/hybrid arms need.
2. Free end-to-end check: the scripted offline LLM, no API calls, ~3 minutes. It writes
   `runs/summary.md` with every table filled (all numbers are meaningless).
3. Real smoke test: 8 stratified tasks × 2 arms, about $2.40 estimated, to check the key, the judge
   and the caps.
4. The full plan: about **$143** expected; the caps stop it at about **$214** (plus at most
   `--concurrency` in-flight tasks per arm; see [Cost estimates](#cost-estimates)). It asks before spending; `--yes` skips the prompt.

Then paste `runs/summary.md` into the README (see [Results template](#results-template)).

## Before you start

- **API key.** Set `ANTHROPIC_API_KEY` in the shell or in `.env`. Without it `eval run` turns the
  judge off with only a warning on stderr, **skips every L3 task** and scores ADV by rules only; `run_experiments.sh --agent core` refuses to
  start instead.
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
and the runner records what a killed task had spent as its `cost_usd` (tokens stay 0).

`run_experiments.sh` sets each arm's cap to its estimate × `--cap-factor` (default 1.5) and, by
default, stops starting new arms once the total spend reaches the sum of those caps
(`--max-total-usd` to lower it). An arm that hit its cap makes the script exit 1 and is listed
under *incomplete arms*; its accuracy is over fewer tasks and **should not be compared** until
re-run with a higher cap.

The per-run token budget (`AGENT_TOKEN_BUDGET`, 400k tokens per task) still applies inside each
task.

## Cost estimates

Estimated with `core/llm.py`'s price table (USD per 1M tokens, input/output: Haiku 5.5 0.10/0.50,
Sonnet 5.5 2.00/10.00). Regenerate after a price or plan change:

```bash
uv run python -m analyst_agent.eval.experiments plan
```

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

To rebuild the summary from the latest invocation's manifest (for example after re-running one
arm and editing its `run_dir` in the manifest):

```bash
uv run python -m analyst_agent.eval.experiments summary "$(ls -d runs/experiments/2*/ | tail -n 1)manifest.tsv" --out runs/summary.md
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

`runs/summary.md` has this shape (all numbers below are placeholders). Paste the *Overview*
table and the experiment tables you want into the README, and fill in the findings by hand.

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
| *Notes* says "N not started (budget)" | The arm hit its `--budget-usd`. Re-run that arm alone with `--budget-usd` raised (or `--cap-factor 2`), then point the manifest row at the new run. |
| *Retrieval errors* > 0% in embedding/hybrid | `sentence-transformers` missing or the model download failed. `uv sync --extra embeddings`, warm with `python -m analyst_agent.semantic eval -b embedding`. |
| Many timeouts on L3 | Raise `--timeout`, or lower `--concurrency` if 429 retries are stretching tasks. |
| Script exits 1 but `runs/summary.md` exists | Some arm is incomplete; the summary marks it in *Notes*. See the list the script prints and `<arm>.log`. |
