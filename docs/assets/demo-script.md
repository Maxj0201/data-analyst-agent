# 3-minute live demo script

Audience: an interviewer who has not seen the project. Goal: show that the agent is
*controllable* (approval gate), *checkable* (critic) and *observable* (trace), not just that it
answers questions.

## Before you start (not part of the 3 minutes)

```bash
export ANTHROPIC_API_KEY=sk-ant-...
AGENT_ROUTING_ENABLED=true AGENT_SEMANTIC_LAYER_ENABLED=true AGENT_APPROVAL_ENABLED=true \
AGENT_APPROVAL_ROW_THRESHOLD=0 AGENT_TRACE_STORE_ENABLED=true scripts/demo.sh
```

- `AGENT_APPROVAL_ROW_THRESHOLD=0` gates every `SELECT`, so the approval card always appears.
  Without it only large queries, `run_python` and budget overruns are gated.
- Open http://localhost:8501 and the terminal side by side. Run the question once beforehand
  so the Docker images, DuckDB and fonts are warm; a cold first call is slower.
- Fallback with no network or key: `scripts/ui-e2e/run.sh /tmp/ui-e2e` drives the same UI with
  a scripted LLM (the screenshots in the README come from it). Say so if you use it.
- The trace in `docs/assets/trace-example.txt` and the screenshots show a *dry run*: costs are
  $0 and the model is `dry-run`. A live run shows real tokens, cost and model names.

## Script

| Time | Do (click / type) | Say |
|---|---|---|
| 0:00 | Show the UI. Sidebar: sample dataset **SaaS subscriptions** is preselected. | "This answers a plain-English question over a table. The point isn't the answer, it's that every step is gated, checked and traceable." |
| 0:20 | Click the example question **Which region has the highest MRR?** and press **Ask**. | "I'm picking a boring question on purpose, so you can judge the *process*." |
| 0:30 | Point at the timeline as it streams: **ROUTE**, **METRICS**, **PLAN**. | "A cheap router call labels difficulty and picks the model, L1/L2/L3. The semantic layer injects governed metric definitions, so 'MRR' means what finance says it means." |
| 0:55 | The **Approval required** card appears. Point at the SQL and the reason line (`EXPLAIN estimates … rows`). Type a note, e.g. "fine, go". Click **Approve**. | "The loop paused before running SQL. The gate keys off DuckDB's `EXPLAIN` estimate, and any `run_python`. Through the API a 5-minute timeout counts as deny." |
| 1:15 | (Optional, 20 s) Re-ask, click **Deny** with the note "use a sample". Point at the denial appearing as a tool error and the agent re-planning. | "A denial goes back to the model as a tool error, so it re-plans. It never escalates to a bigger model on a human no." |
| 1:35 | Point at **CRITIC: pass** (or `revise`), then the **Report** tab: key numbers, table, chart. | "A separate call reviews the draft. It sees the SQL and the result tables but not the planner's reasoning. A deterministic check runs first and flags headline numbers that no tool output contains." |
| 2:00 | Switch to the terminal: `uv run analyst-agent trace show <run_id>` (or `trace ls`, then show the newest). Use `docs/assets/trace-example.txt` as a backup. | "Same run as a span tree." |
| 2:10 | Point at, in order: `route L2 → …`; `semantic_retrieval`; `tool:run_sql` with `approval:run_sql approved by …` under it; `critic round=1 verdict=…`; the last two lines (total cost and `prompts:` versions). | "Every node has cost, model, tier and prompt version. The prompt list at the bottom is pinned: a test fails if a prompt's text changes without a version bump, so I can diff two eval runs by prompt." |
| 2:40 | Say the closing line. Open the README results table only if asked. | "Confinement is kernel-level (seccomp + Landlock) and all of this is gated by an 80-task benchmark in CI. I haven't published live-model accuracy numbers yet; that is the first roadmap item, and I'd rather say so than quote a number I haven't measured." |

## Likely follow-ups (one-line answers)

- *What if the data contains an injection?* The finance sample has a file name that tries
  to redirect the agent; tool output is fenced as untrusted and the ADV tasks test it
  (`docs/design-decisions.md`, part 1, item 5).
- *Why a hand-written loop?* Explicit control of approval, critic rounds and escalation;
  see `docs/design-decisions.md`, item 1.
- *Cost?* Per model and per role in the **Cost & tokens** tab and the trace.

## If something goes wrong

- The approval card never shows: the threshold env var was not passed to the API container;
  restart with the command above.
- The model call fails: the key is missing from `.env` or the shell. Switch to the scripted
  fallback and say it is scripted.
- The question takes more than ~30 s: narrate the trace sections above while it runs.
