# Autonomous Data Analyst Agent

A Python agent that answers natural-language questions over tabular data (CSV / Parquet / SQLite / DuckDB files) by inspecting schema, writing & executing SQL/Python in a sandbox, verifying results with a critic step, and producing a Markdown report with charts. Built on the Anthropic Messages API with a hand-written agent loop; LangGraph may be used only for state management.

## Repo conventions (follow exactly so parallel sessions merge cleanly)
- Python 3.11, uv for dependency management, pyproject.toml at root
- Package layout: src/analyst_agent/{core,tools,sandbox,eval,api}/
- All LLM calls go through src/analyst_agent/core/llm.py (single wrapper: model name, max_tokens, prompt caching, token/cost accounting)
- Config via pydantic-settings, env vars prefixed AGENT_ (AGENT_MODEL, AGENT_MAX_TURNS, AGENT_TOKEN_BUDGET)
- Tests in tests/ with pytest; mock the LLM in tests, never call the real API
- Type hints everywhere, ruff for lint/format, line length 100
- Work on your own branch named after your task, commit in small logical steps, open a PR when done
- Do not edit files outside your scope unless necessary; if you must, note it in the PR description
- Interfaces between packages: sandbox/__init__.py exposes run_sql(query) -> SqlResult and run_python(code) -> PyResult as dataclasses. If the real implementation isn't present, provide a stub with the same signature.

## Sandbox access: module-level vs per-job
- Single-run callers (the CLI) use the module-level `sandbox.run_sql` / `sandbox.run_python` after `sandbox.load_data(...)`. State is process-global.
- Concurrent callers (the API job pool, the eval runner) must not share that global. They create one sandbox per job with `tools.create_sandbox(datasets, artifacts_dir=...)`, pass it as `ToolContext(tables=sb.tables, sandbox=sb)`, and close it afterwards. Tool handlers call `ctx.run_sql` / `ctx.run_python`, which use `ToolContext.sandbox` when set and fall back to the module-level functions otherwise. Both paths return the same `SqlResult` / `PyResult` dataclasses.
