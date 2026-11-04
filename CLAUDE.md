# Autonomous Data Analyst Agent

A Python agent that answers natural-language questions over tabular data (CSV / SQLite / DuckDB) by inspecting schema, writing & executing SQL/Python in a sandbox, verifying results with a critic step, and producing a Markdown report with charts. Built on the Anthropic Messages API with a hand-written agent loop; LangGraph may be used only for state management.

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
