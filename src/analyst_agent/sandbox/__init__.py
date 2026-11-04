"""Safe execution layer for agent-generated SQL and Python.

Cross-package interface (CLAUDE.md)::

    run_sql(query: str) -> SqlResult
    run_python(code: str) -> PyResult

Neither function raises for problems with the query / code being run: every failure is
returned as ``ok=False`` with a readable ``error`` string of the form ``"<Kind>: <message>"``
(kinds in ``sandbox._errors``). See docs/sandbox.md for the threat model.

Usage::

    from analyst_agent import sandbox

    # Per-session object (what analyst_agent.tools.create_sandbox picks up)
    sb = sandbox.create_sandbox({"orders": Path("orders.csv")})
    sb.run_sql("SELECT count(*) AS n FROM orders")      # -> SqlResult
    sb.run_python("print(tables['orders'].describe())")  # -> PyResult

    # Module-level functions over a process-wide default dataset
    sandbox.load_data({"orders": "orders.csv"})
    sandbox.run_sql("SELECT ...")
    sandbox.run_python("...")
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


# The runners import the dataclasses above from this package, so they are imported after them.
import threading  # noqa: E402
from pathlib import Path  # noqa: E402

from ._errors import NO_DATA, fmt  # noqa: E402
from ._loaders import DataLoadError, DataSources  # noqa: E402
from .duckdb_runner import DuckDBRunner, validate_query  # noqa: E402
from .python_runner import run_python as _run_python_subprocess  # noqa: E402

__all__ = [
    "DataLoadError",
    "DuckDBRunner",
    "PyResult",
    "Sandbox",
    "SqlResult",
    "create_sandbox",
    "current_tables",
    "load_data",
    "reset",
    "run_python",
    "run_sql",
    "validate_query",
]


class Sandbox:
    """A dataset bound to a read-only DuckDB database and the Python subprocess runner."""

    def __init__(self, datasets: DataSources | None = None, **sql_options: object) -> None:
        self._sql = DuckDBRunner(datasets, **sql_options)  # type: ignore[arg-type]

    @property
    def tables(self) -> list[str]:
        return self._sql.tables

    @property
    def datasets(self) -> dict[str, Path]:
        return self._sql.sources

    def add_data(self, datasets: DataSources) -> list[str]:
        return self._sql.load(datasets)

    def run_sql(self, query: str) -> SqlResult:
        return self._sql.run_sql(query)

    def run_python(self, code: str) -> PyResult:
        return _run_python_subprocess(code, self._sql.sources)

    def close(self) -> None:
        self._sql.close()

    def __enter__(self) -> Sandbox:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def create_sandbox(datasets: dict[str, Path]) -> Sandbox:
    """Factory used by ``analyst_agent.tools.sandbox_iface.create_sandbox``.

    ``datasets`` maps table name -> CSV / Parquet / SQLite path (a SQLite file contributes
    one table per SQLite table, named after that table).
    """
    return Sandbox(datasets)


_lock = threading.Lock()
_default: Sandbox | None = None


def load_data(sources: DataSources, *, replace: bool = True) -> list[str]:
    """Register data files for the module-level ``run_sql`` / ``run_python``.

    Returns the table names. Raises :class:`DataLoadError` for missing/unsupported files
    (a trusted-caller error, not an untrusted-input error).
    """
    global _default
    with _lock:
        if _default is None or replace:
            if _default is not None:
                _default.close()
            _default = Sandbox(sources)
        else:
            _default.add_data(sources)
        return _default.tables


def current_tables() -> list[str]:
    return _default.tables if _default is not None else []


def reset() -> None:
    """Drop all module-level data."""
    global _default
    with _lock:
        if _default is not None:
            _default.close()
        _default = None


def run_sql(query: str) -> SqlResult:
    """Run one read-only SQL statement (30s timeout, 200-row cap) on the loaded data."""
    sb = _default
    if sb is None:
        return SqlResult(ok=False, error=fmt(NO_DATA, "No data loaded; call load_data(...) first."))
    return sb.run_sql(query)


def run_python(code: str) -> PyResult:
    """Run Python in an isolated subprocess (60s timeout, memory-limited, no network).

    The data registered with :func:`load_data` is available as ``tables[name]`` / ``df``.
    """
    sb = _default
    return sb.run_python(code) if sb is not None else _run_python_subprocess(code, None)
