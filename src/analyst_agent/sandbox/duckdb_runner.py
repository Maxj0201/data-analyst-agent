"""Read-only DuckDB execution for agent-written SQL.

Defense layers (any one of them blocks a write; see docs/sandbox.md):

1. **Statement allowlist** - exactly one statement whose leading keyword is SELECT, WITH or
   EXPLAIN and which DuckDB's own parser classifies as SELECT (or EXPLAIN of a SELECT).
2. **Read-only database** - data is loaded into a private DuckDB file in a temp dir, which is
   then re-opened with ``read_only=True``. DDL/DML fail inside the engine even if (1) is
   bypassed.
3. **No external access, locked config** - ``enable_external_access=false`` (no file reads,
   COPY, ATTACH, HTTP, extension install/load) and ``lock_configuration=true`` so SQL cannot
   turn it back on.
4. **Resource limits** - wall-clock timeout via ``interrupt()``, DuckDB ``memory_limit``,
   and a row cap on what is returned to the caller.
"""

from __future__ import annotations

import math
import re
import tempfile
import threading
import time
import weakref
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

from . import SqlResult
from . import _errors as T
from ._loaders import DataSources, load_into_duckdb, normalize_sources

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_ROW_LIMIT = 200
DEFAULT_MEMORY_LIMIT = "2GB"
MARKDOWN_PREVIEW_ROWS = 20
_FETCH_CHUNK = 10_000

ALLOWED_LEADING_KEYWORDS = ("SELECT", "WITH", "EXPLAIN")
_ALLOWED_MSG = (
    "Only read-only queries are allowed: a single SELECT, WITH ... SELECT, or EXPLAIN SELECT "
    "statement."
)

_LEADING_NOISE = re.compile(r"\A(?:\s+|--[^\n]*(?:\n|\Z)|/\*.*?\*/|\()*", re.DOTALL)
_KEYWORD = re.compile(r"[A-Za-z_]+")


class StatementNotAllowed(ValueError):
    pass


def _strip_leading_noise(sql: str) -> str:
    return _LEADING_NOISE.sub("", sql, count=1)


def _leading_keyword(sql: str) -> str:
    m = _KEYWORD.match(_strip_leading_noise(sql))
    return m.group(0).upper() if m else ""


def validate_query(con: duckdb.DuckDBPyConnection, query: str) -> None:
    """Raise :class:`StatementNotAllowed` unless ``query`` is a single read-only statement."""
    if not query or not query.strip():
        raise StatementNotAllowed("Empty query.")

    keyword = _leading_keyword(query)
    if keyword not in ALLOWED_LEADING_KEYWORDS:
        raise StatementNotAllowed(
            f"Statement starting with {keyword or '<unknown>'!r} is not allowed. {_ALLOWED_MSG}"
        )

    try:
        statements = con.extract_statements(query)
    except duckdb.Error as exc:
        raise StatementNotAllowed(f"Could not parse query: {exc}") from exc

    if len(statements) != 1:
        raise StatementNotAllowed(
            f"Exactly one statement is allowed, got {len(statements)}. {_ALLOWED_MSG}"
        )

    stmt_type = statements[0].type
    if stmt_type == duckdb.StatementType.SELECT:
        return
    if stmt_type == duckdb.StatementType.EXPLAIN:
        # EXPLAIN [ANALYZE] <inner>: the inner statement must itself be a SELECT.
        inner = _strip_leading_noise(query)
        inner = re.sub(r"\AEXPLAIN\s+(?:ANALYZE\s+)?", "", inner, flags=re.IGNORECASE)
        try:
            inner_stmts = con.extract_statements(inner)
        except duckdb.Error as exc:
            raise StatementNotAllowed(f"Could not parse EXPLAIN target: {exc}") from exc
        if len(inner_stmts) == 1 and inner_stmts[0].type == duckdb.StatementType.SELECT:
            return
        raise StatementNotAllowed(f"EXPLAIN is only allowed for SELECT queries. {_ALLOWED_MSG}")

    raise StatementNotAllowed(f"Statement type {stmt_type.name} is not allowed. {_ALLOWED_MSG}")


def _ms_since(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _jsonable(value: Any) -> Any:
    """Keep JSON primitives; stringify everything else (Decimal, dates, lists, blobs...)."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def _format_cell(value: Any) -> str:
    if value is None:
        return "NULL"
    text = str(value).replace("\r", " ").replace("\n", " ").replace("|", "\\|")
    return text if len(text) <= 80 else text[:77] + "..."


def to_markdown(
    columns: Sequence[str],
    rows: Sequence[dict[str, Any]],
    total: int | None = None,
    max_rows: int = MARKDOWN_PREVIEW_ROWS,
) -> str:
    """Render rows as a GitHub-flavored Markdown table with a truncation note."""
    if not columns:
        return "_(no columns)_"
    lines = [
        "| " + " | ".join(_format_cell(c) for c in columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in rows[:max_rows]:
        lines.append("| " + " | ".join(_format_cell(row.get(c)) for c in columns) + " |")
    total = len(rows) if total is None else total
    if total == 0:
        lines.append("\n_(0 rows)_")
    elif total > min(len(rows), max_rows):
        lines.append(f"\n_(showing {min(len(rows), max_rows)} of {total} rows)_")
    return "\n".join(lines)


def _dedupe(columns: Sequence[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for c in columns:
        if c in seen:
            seen[c] += 1
            out.append(f"{c}_{seen[c]}")
        else:
            seen[c] = 0
            out.append(c)
    return out


class DuckDBRunner:
    """Holds a private read-only DuckDB database built from data files."""

    def __init__(
        self,
        sources: DataSources | None = None,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        row_limit: int = DEFAULT_ROW_LIMIT,
        memory_limit: str = DEFAULT_MEMORY_LIMIT,
    ) -> None:
        self.timeout_s = timeout_s
        self.row_limit = row_limit
        self.memory_limit = memory_limit
        self._tmp = tempfile.TemporaryDirectory(prefix="analyst-duckdb-")
        self._db_path = Path(self._tmp.name) / "data.duckdb"
        self._sources: dict[str, Path] = {}
        self._tables: list[str] = []
        self._conn: duckdb.DuckDBPyConnection | None = None
        self._lock = threading.Lock()
        self._finalizer = weakref.finalize(self, self._tmp.cleanup)
        self.load(sources)

    # -- data loading (trusted path) -------------------------------------------------------
    def load(self, sources: DataSources | None) -> list[str]:
        """Add data sources and rebuild the read-only database. Returns all table names."""
        new = normalize_sources(sources)
        with self._lock:
            merged = {**self._sources, **new}
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            if self._db_path.exists():
                self._db_path.unlink()
            build = duckdb.connect(str(self._db_path))
            try:
                tables = load_into_duckdb(build, merged)
            finally:
                build.close()
            self._sources, self._tables = merged, tables
            self._conn = duckdb.connect(
                str(self._db_path),
                read_only=True,
                config={
                    "enable_external_access": False,
                    "autoinstall_known_extensions": False,
                    "autoload_known_extensions": False,
                    "allow_community_extensions": False,
                    "memory_limit": self.memory_limit,
                    "temp_directory": str(Path(self._tmp.name) / "spill"),
                    "lock_configuration": True,
                },
            )
        return list(self._tables)

    @property
    def tables(self) -> list[str]:
        return list(self._tables)

    @property
    def sources(self) -> dict[str, Path]:
        return dict(self._sources)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
        self._finalizer()

    # -- query execution (untrusted path) --------------------------------------------------
    def run_sql(
        self, query: str, *, timeout_s: float | None = None, row_limit: int | None = None
    ) -> SqlResult:
        timeout_s = self.timeout_s if timeout_s is None else timeout_s
        row_limit = self.row_limit if row_limit is None else row_limit
        start = time.monotonic()

        def fail(kind: str, message: str) -> SqlResult:
            return SqlResult(ok=False, error=T.fmt(kind, message), duration_ms=_ms_since(start))

        if self._conn is None:
            return fail(T.NO_DATA, "No database is open; load data first.")

        cur = self._conn.cursor()
        try:
            try:
                validate_query(cur, query)
            except StatementNotAllowed as exc:
                return fail(T.STATEMENT_NOT_ALLOWED, str(exc))

            timed_out = threading.Event()

            def _interrupt() -> None:
                timed_out.set()
                cur.interrupt()

            timer = threading.Timer(timeout_s, _interrupt)
            timer.daemon = True
            timer.start()
            try:
                cur.execute(query)
                columns = _dedupe([d[0] for d in cur.description or []])
                head = cur.fetchmany(row_limit)
                total = len(head)
                if len(head) == row_limit:
                    while chunk := cur.fetchmany(_FETCH_CHUNK):
                        total += len(chunk)
                        if timed_out.is_set():
                            raise duckdb.InterruptException("timeout")
            finally:
                timer.cancel()
        except duckdb.InterruptException:
            return fail(T.TIMEOUT, f"Query exceeded the {timeout_s:g}s time limit and was stopped.")
        except duckdb.PermissionException as exc:
            return fail(T.PERMISSION_DENIED, str(exc))
        except duckdb.OutOfMemoryException as exc:
            return fail(T.MEMORY_LIMIT, str(exc))
        except duckdb.Error as exc:
            if timed_out.is_set():
                return fail(T.TIMEOUT, f"Query exceeded the {timeout_s:g}s time limit.")
            return fail(T.QUERY_ERROR, str(exc))
        finally:
            cur.close()

        rows = [dict(zip(columns, map(_jsonable, r), strict=True)) for r in head]
        return SqlResult(
            ok=True,
            columns=columns,
            rows=rows,
            row_count=total,
            truncated=total > len(rows),
            markdown_preview=to_markdown(columns, rows, total),
            duration_ms=_ms_since(start),
        )
