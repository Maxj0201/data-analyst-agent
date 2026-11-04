Read CLAUDE.md first. Scope: src/analyst_agent/core/ and src/analyst_agent/tools/. Branch: feat/agent-core.

Build:
1. core/llm.py — thin wrapper around anthropic.Anthropic().messages.create with: model from config, system prompt cache_control, retries with backoff, and a CostTracker that accumulates input/output/cache tokens and estimated USD per run.
2. core/loop.py — the agent loop: Plan → Execute (tool calls) → Verify → Revise. Max turns from config; hard stop when token budget exceeded (return partial result with a clear status, never raise). State is a dataclass holding messages, tool results, plan, and cost.
3. core/critic.py — a separate LLM call with its own system prompt that reviews the draft answer + the executed SQL/code + result tables and returns structured JSON: {verdict: pass|revise, issues: [...], suggested_fix: str}. The loop re-plans on "revise", max 2 critic rounds.
4. tools/ — tool definitions as JSON schemas plus Python handlers, registered in tools/registry.py:
   - inspect_schema(table?) → columns, dtypes, row count, 5 sample rows, null rates
   - run_sql(query) → calls sandbox.run_sql (interface defined in CLAUDE.md; provide a stub if sandbox isn't implemented yet)
   - run_python(code) → same pattern via sandbox.run_python
   - make_chart(spec) → matplotlib PNG saved to artifacts/, returns path
   - final_report(markdown, key_numbers: dict) → terminates the loop
   Wrap every tool result in clear delimiters and label it as untrusted data in the prompt, so instructions embedded in data are not followed.
5. core/prompts.py — system prompts for planner, critic, report writer as constants.
6. cli.py — typer app: `analyst-agent ask --data path.csv "question"` prints the report and cost summary.
7. Unit tests with a mocked LLM covering: normal flow, critic-triggered revision, token-budget stop, tool error handling.
Open a PR when done.
