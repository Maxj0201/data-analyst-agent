Read CLAUDE.md first. Scope: src/analyst_agent/sandbox/. Branch: feat/sandbox.

Build a safe execution layer:
1. sandbox/duckdb_runner.py — load CSV/SQLite/Parquet into an in-memory DuckDB; run_sql(query) with: read-only connection, statement allowlist (SELECT/WITH/EXPLAIN only; reject anything else with a clear error), 30s timeout, row cap (return first 200 rows + total count), results as list-of-dicts plus a Markdown preview.
2. sandbox/python_runner.py — run_python(code, data_context) in a subprocess with: separate interpreter, 60s wall-clock timeout, memory limit via resource.setrlimit, restricted env (no network: block socket at import time via sitecustomize), only pandas/numpy/matplotlib/scipy available, cwd set to a temp dir, stdout/stderr captured, exceptions returned as structured errors not raised.
3. sandbox/__init__.py — implement run_sql(query) -> SqlResult and run_python(code) -> PyResult dataclasses exactly as CLAUDE.md specifies.
4. Security tests: SQL injection attempts, DROP/DELETE statements, infinite loops, memory bombs, attempted file writes outside temp dir, attempted network calls — all must be blocked and return errors.
5. docs/sandbox.md explaining the threat model and what is and isn't protected against.
Open a PR when done.
