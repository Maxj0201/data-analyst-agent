"""Sandbox interface (per CLAUDE.md).

Exposes ``run_sql(query) -> SqlResult`` and ``run_python(code) -> PyResult``.

THIS IS A STUB, added by feat/agent-core so the agent can run before the real sandbox lands
(feat/sandbox). The two public functions and the two dataclasses are the contract the tools rely
on; feat/sandbox should keep their signatures and fields and replace the bodies.

``load_data`` is an addition CLAUDE.md does not specify: with module-level ``run_sql`` /
``run_python`` there has to be some way to tell the sandbox which files to expose as tables.

Errors are returned in the result (``ok=False, error=...``), never raised.

Stub limitations: the SQL side uses an in-memory DuckDB behind a SELECT/WITH/EXPLAIN allowlist and
a 200-row cap. The Python side is a plain subprocess with a timeout and is NOT a security boundary.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["PyResult", "SqlResult", "load_data", "loaded_tables", "run_python", "run_sql"]

ROW_CAP = 200
PYTHON_TIMEOUT_S = 60.0


@dataclass
class SqlResult:
    ok: bool
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)  # first ROW_CAP rows
    total_rows: int = 0
    truncated: bool = False
    preview_markdown: str = ""
    error: str | None = None


@dataclass
class PyResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    error: str | None = None


# --------------------------------------------------------------------------- stub state

_datasets: dict[str, Path] = {}
_con: Any = None  # duckdb.DuckDBPyConnection


def load_data(datasets: Mapping[str, str | Path]) -> list[str]:
    """Replace the loaded data with ``datasets`` (table name -> CSV/Parquet path)."""
    import duckdb

    global _con
    con = duckdb.connect(":memory:")
    for name, path in datasets.items():
        reader = "read_parquet" if str(path).lower().endswith(".parquet") else "read_csv_auto"
        ident = '"' + name.replace('"', '""') + '"'
        con.execute(f"CREATE TABLE {ident} AS SELECT * FROM {reader}(?)", [str(path)])
    if _con is not None:
        _con.close()
    _con = con
    _datasets.clear()
    _datasets.update({k: Path(v) for k, v in datasets.items()})
    return list(_datasets)


def loaded_tables() -> list[str]:
    return list(_datasets)


# --------------------------------------------------------------------------- run_sql

_ALLOWED_SQL = re.compile(r"^\s*(select|with|explain)\b", re.IGNORECASE)


def _preview(columns: list[str], rows: list[dict[str, Any]], limit: int = 20) -> str:
    def cell(v: Any) -> str:
        return "" if v is None else str(v).replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    lines += ["| " + " | ".join(cell(r.get(c)) for c in columns) + " |" for r in rows[:limit]]
    if len(rows) > limit:
        lines.append(f"... ({len(rows) - limit} more rows not shown)")
    return "\n".join(lines)


def _jsonable(v: Any) -> Any:
    return v if v is None or isinstance(v, (bool, int, float, str)) else str(v)


def run_sql(query: str) -> SqlResult:
    if _con is None:
        return SqlResult(ok=False, error="No data loaded.")
    q = query.strip().rstrip(";").strip()
    if not _ALLOWED_SQL.match(q):
        return SqlResult(ok=False, error="Only SELECT / WITH / EXPLAIN statements are allowed.")
    if ";" in q:
        return SqlResult(ok=False, error="Multiple statements are not allowed.")
    try:
        cur = _con.execute(q)
        columns = [d[0] for d in cur.description or []]
        fetched = cur.fetchmany(ROW_CAP + 1)
        truncated = len(fetched) > ROW_CAP
        total = len(fetched)
        if truncated:
            total = _con.execute(f"SELECT COUNT(*) FROM ({q}) AS _sub").fetchone()[0]
            fetched = fetched[:ROW_CAP]
        rows = [{c: _jsonable(v) for c, v in zip(columns, r, strict=True)} for r in fetched]
    except Exception as exc:  # noqa: BLE001 - returned as a structured error
        return SqlResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    return SqlResult(
        ok=True,
        columns=columns,
        rows=rows,
        total_rows=total,
        truncated=truncated,
        preview_markdown=_preview(columns, rows),
    )


# --------------------------------------------------------------------------- run_python


def run_python(code: str) -> PyResult:
    paths = json.dumps({k: str(v) for k, v in _datasets.items()})
    prelude = (
        "import json, pandas as pd, numpy as np\n"
        f"_paths = json.loads({paths!r})\n"
        "tables = {k: (pd.read_parquet(p) if p.endswith('.parquet') else pd.read_csv(p))"
        " for k, p in _paths.items()}\n"
        "df = next(iter(tables.values())) if len(tables) == 1 else None\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "snippet.py"
        script.write_text(prelude + code)
        try:
            proc = subprocess.run(
                [sys.executable, "-I", str(script)],
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=PYTHON_TIMEOUT_S,
                env={"PATH": "/usr/bin:/bin", "MPLBACKEND": "Agg"},
            )
        except subprocess.TimeoutExpired:
            return PyResult(ok=False, error=f"Timed out after {PYTHON_TIMEOUT_S:.0f}s")
    if proc.returncode != 0:
        tail = proc.stderr.strip().splitlines()[-1:] or ["unknown error"]
        return PyResult(ok=False, stdout=proc.stdout, stderr=proc.stderr, error=tail[0])
    return PyResult(ok=True, stdout=proc.stdout, stderr=proc.stderr)
