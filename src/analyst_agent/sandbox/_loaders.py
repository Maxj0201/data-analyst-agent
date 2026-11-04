"""Turn CSV / Parquet / SQLite / DuckDB files into named tables.

Runs in the trusted parent process. Data files are treated as untrusted *data*:
names derived from file names / SQLite table names are sanitized into safe identifiers
and every identifier is quoted before being interpolated into SQL.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

import duckdb
import pandas as pd

CSV_SUFFIXES = {".csv", ".tsv", ".txt"}
PARQUET_SUFFIXES = {".parquet", ".pq"}
SQLITE_SUFFIXES = {".sqlite", ".sqlite3", ".db"}
DUCKDB_SUFFIXES = {".duckdb", ".ddb"}
SUPPORTED_SUFFIXES = CSV_SUFFIXES | PARQUET_SUFFIXES | SQLITE_SUFFIXES | DUCKDB_SUFFIXES

_IDENT_RE = re.compile(r"\W+")

DataSources = Mapping[str, str | Path] | Iterable[str | Path] | str | Path


class DataLoadError(ValueError):
    """Raised (to the trusted caller, at load time) when a data source is unusable."""


def safe_identifier(name: str) -> str:
    """Map an arbitrary string to an identifier (same rule as tools.table_name_for)."""
    ident = _IDENT_RE.sub("_", name).strip("_").lower() or "data"
    return f"t_{ident}" if ident[0].isdigit() else ident


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def normalize_sources(sources: DataSources | None) -> dict[str, Path]:
    """Accept a path, a list of paths, or a {name: path} mapping; return {name: path}.

    For SQLite / DuckDB database files the name is a hint only (each table in the file becomes
    its own table).
    """
    if sources is None:
        return {}
    if isinstance(sources, str | Path):
        sources = [sources]
    if isinstance(sources, Mapping):
        items = [(safe_identifier(str(k)), Path(v)) for k, v in sources.items()]
    else:
        items = [(safe_identifier(Path(p).stem), Path(p)) for p in sources]
    out: dict[str, Path] = {}
    for name, path in items:
        if not path.is_file():
            raise DataLoadError(f"data file not found: {path}")
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise DataLoadError(
                f"unsupported file type {path.suffix!r} for {path.name}; "
                f"supported: {sorted(SUPPORTED_SUFFIXES)}"
            )
        if name in out:
            raise DataLoadError(f"duplicate table name {name!r}")
        out[name] = path.resolve()
    return out


def _sqlite_tables(path: Path) -> list[str]:
    uri = f"file:{path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as con:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    return [r[0] for r in rows]


def _read_sqlite_table(path: Path, table: str) -> pd.DataFrame:
    uri = f"file:{path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as con:
        return pd.read_sql_query(f"SELECT * FROM {quote_ident(table)}", con)


def _duckdb_tables(path: Path) -> list[tuple[str, str]]:
    """(schema, table) for every base table in a DuckDB file, opened read-only."""
    try:
        con = duckdb.connect(str(path), read_only=True)
    except duckdb.Error as exc:
        raise DataLoadError(f"cannot open DuckDB file {path.name}: {exc}") from exc
    try:
        return [
            (r[0], r[1])
            for r in con.execute(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_type = 'BASE TABLE' ORDER BY 1, 2"
            ).fetchall()
        ]
    finally:
        con.close()


def _duckdb_table_name(schema: str, table: str) -> str:
    return safe_identifier(table if schema == "main" else f"{schema}_{table}")


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _copy_duckdb_tables(
    con: duckdb.DuckDBPyConnection, path: Path, create: Callable[[str, str], None]
) -> None:
    """ATTACH the file read-only and call ``create(table_name, source_sql)`` per base table."""
    tables = _duckdb_tables(path)
    con.execute(f"ATTACH {_sql_literal(str(path))} AS __src (READ_ONLY)")
    try:
        for schema, table in tables:
            create(
                _duckdb_table_name(schema, table),
                f"SELECT * FROM __src.{quote_ident(schema)}.{quote_ident(table)}",
            )
    finally:
        con.execute("DETACH __src")


def load_into_duckdb(con: duckdb.DuckDBPyConnection, sources: Mapping[str, Path]) -> list[str]:
    """Create one table per source in a *writable* build connection. Returns table names."""
    created: list[str] = []

    def _check(name: str) -> str:
        if name in created:
            raise DataLoadError(f"duplicate table name {name!r}")
        created.append(name)
        return quote_ident(name)

    for name, path in sources.items():
        suffix = path.suffix.lower()
        if suffix in CSV_SUFFIXES:
            con.execute(
                f"CREATE TABLE {_check(name)} AS SELECT * FROM read_csv_auto(?)", [str(path)]
            )
        elif suffix in PARQUET_SUFFIXES:
            con.execute(
                f"CREATE TABLE {_check(name)} AS SELECT * FROM read_parquet(?)", [str(path)]
            )
        elif suffix in DUCKDB_SUFFIXES:  # one table per base table in the file

            def _create(table: str, select: str) -> None:
                con.execute(f"CREATE TABLE {_check(table)} AS {select}")

            _copy_duckdb_tables(con, path, _create)
        else:  # SQLite: one DuckDB table per SQLite table, read via the stdlib driver
            for table in _sqlite_tables(path):
                df = _read_sqlite_table(path, table)
                con.register("__sqlite_import", df)
                con.execute(
                    f"CREATE TABLE {_check(safe_identifier(table))} AS "
                    "SELECT * FROM __sqlite_import"
                )
                con.unregister("__sqlite_import")
    return created


def load_dataframes(sources: Mapping[str, Path]) -> dict[str, pd.DataFrame]:
    """Load every source into pandas DataFrames keyed by table name."""
    frames: dict[str, pd.DataFrame] = {}
    con = duckdb.connect(":memory:")
    try:
        for name, path in sources.items():
            suffix = path.suffix.lower()
            if suffix in CSV_SUFFIXES:
                frames[name] = con.execute("SELECT * FROM read_csv_auto(?)", [str(path)]).df()
            elif suffix in PARQUET_SUFFIXES:
                frames[name] = con.execute("SELECT * FROM read_parquet(?)", [str(path)]).df()
            elif suffix in DUCKDB_SUFFIXES:

                def _fetch(table: str, select: str) -> None:
                    frames[table] = con.execute(select).df()

                _copy_duckdb_tables(con, path, _fetch)
            else:
                for table in _sqlite_tables(path):
                    frames[safe_identifier(table)] = _read_sqlite_table(path, table)
    finally:
        con.close()
    return frames
