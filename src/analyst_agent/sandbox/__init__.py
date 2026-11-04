"""Safe execution layer for agent-generated SQL and Python.

Cross-package interface (CLAUDE.md)::

    run_sql(query: str) -> SqlResult
    run_python(code: str) -> PyResult

Neither function raises for problems with the query / code being run: every failure is
returned as ``ok=False`` with a readable ``error`` string of the form ``"<Kind>: <message>"``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SqlResult:
    ok: bool
    rows: list[dict[str, Any]] = field(default_factory=list)  # first 200 rows, JSON-safe
    columns: list[str] = field(default_factory=list)
    row_count: int = 0  # total rows the query produced, before the row cap
    truncated: bool = False  # row_count > len(rows)
    markdown_preview: str = ""
    error: str | None = None
    duration_ms: int = 0


@dataclass
class PyResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    artifacts: list[str] = field(default_factory=list)  # paths of files produced (e.g. PNGs)
    error: str | None = None
    duration_ms: int = 0


def run_sql(query: str) -> SqlResult:
    """Stub until the DuckDB runner lands on this branch."""
    return SqlResult(ok=False, error="NotImplemented: sandbox.run_sql is not implemented yet")


def run_python(code: str) -> PyResult:
    """Stub until the subprocess runner lands on this branch."""
    return PyResult(ok=False, error="NotImplemented: sandbox.run_python is not implemented yet")
