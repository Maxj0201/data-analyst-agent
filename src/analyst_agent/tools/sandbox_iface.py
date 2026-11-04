"""Sandbox interface used by the tools, plus a local stub until ``analyst_agent.sandbox`` lands.

Expected contract for the real sandbox (feat/sandbox):

    from analyst_agent.sandbox import create_sandbox
    sb = create_sandbox(datasets: dict[str, Path])   # table name -> CSV/Parquet/SQLite path
    sb.run_sql(query: str) -> SqlResult
    sb.run_python(code: str) -> PyResult

Errors are *returned* in the result (``ok=False, error=...``), never raised.

The :class:`LocalStubSandbox` below is a development convenience only. Its SQL side is
reasonably safe (in-memory DuckDB, SELECT/WITH/EXPLAIN allowlist), but its Python side is
just a subprocess with a timeout and is NOT a security boundary.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

ROW_CAP = 200


@dataclass
class SqlResult:
    ok: bool
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
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


class Sandbox(Protocol):
    def run_sql(self, query: str) -> SqlResult: ...

    def run_python(self, code: str) -> PyResult: ...


def table_name_for(path: Path) -> str:
    name = re.sub(r"\W+", "_", path.stem).strip("_").lower() or "data"
    return f"t_{name}" if name[0].isdigit() else name


def rows_to_markdown(columns: list[str], rows: list[dict[str, Any]], limit: int = 20) -> str:
    if not columns:
        return "(no columns)"

    def cell(v: Any) -> str:
        return "" if v is None else str(v).replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for r in rows[:limit]:
        lines.append("| " + " | ".join(cell(r.get(c)) for c in columns) + " |")
    if len(rows) > limit:
        lines.append(f"... ({len(rows) - limit} more rows not shown)")
    return "\n".join(lines)


_ALLOWED_SQL = re.compile(r"^\s*(select|with|explain)\b", re.IGNORECASE)


class LocalStubSandbox:
    """In-process DuckDB + subprocess Python. Development stub, not a security boundary."""

    def __init__(self, datasets: dict[str, Path], python_timeout: float = 60.0) -> None:
        import duckdb

        self.datasets = {k: Path(v) for k, v in datasets.items()}
        self.python_timeout = python_timeout
        self.con = duckdb.connect(":memory:")
        for name, path in self.datasets.items():
            suffix = path.suffix.lower()
            reader = "read_parquet" if suffix == ".parquet" else "read_csv_auto"
            self.con.execute(f'CREATE TABLE "{name}" AS SELECT * FROM {reader}(?)', [str(path)])

    def run_sql(self, query: str) -> SqlResult:
        q = query.strip().rstrip(";").strip()
        if not _ALLOWED_SQL.match(q):
            return SqlResult(ok=False, error="Only SELECT / WITH / EXPLAIN statements are allowed.")
        if ";" in q:
            return SqlResult(ok=False, error="Multiple statements are not allowed.")
        try:
            cur = self.con.execute(q)
            columns = [d[0] for d in cur.description or []]
            fetched = cur.fetchmany(ROW_CAP + 1)
            truncated = len(fetched) > ROW_CAP
            total = len(fetched)
            if truncated:
                total = self.con.execute(f"SELECT COUNT(*) FROM ({q}) AS _sub").fetchone()[0]
                fetched = fetched[:ROW_CAP]
            rows = [dict(zip(columns, _jsonable_row(r), strict=True)) for r in fetched]
        except Exception as exc:  # noqa: BLE001 - surfaced to the model as a tool error
            return SqlResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        return SqlResult(
            ok=True,
            columns=columns,
            rows=rows,
            total_rows=total,
            truncated=truncated,
            preview_markdown=rows_to_markdown(columns, rows),
        )

    def run_python(self, code: str) -> PyResult:
        prelude = (
            "import json, pandas as pd, numpy as np\n"
            f"_paths = json.loads({json.dumps(json.dumps({k: str(v) for k, v in self.datasets.items()}))})\n"  # noqa: E501
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
                    timeout=self.python_timeout,
                    env={"PATH": "/usr/bin:/bin", "MPLBACKEND": "Agg"},
                )
            except subprocess.TimeoutExpired:
                return PyResult(ok=False, error=f"Timed out after {self.python_timeout:.0f}s")
        if proc.returncode != 0:
            tail = proc.stderr.strip().splitlines()[-1:] or ["unknown error"]
            return PyResult(ok=False, stdout=proc.stdout, stderr=proc.stderr, error=tail[0])
        return PyResult(ok=True, stdout=proc.stdout, stderr=proc.stderr)


def _jsonable_row(row: tuple[Any, ...]) -> list[Any]:
    out = []
    for v in row:
        if v is None or isinstance(v, (bool, int, float, str)):
            out.append(v)
        else:
            out.append(str(v))
    return out


def create_sandbox(datasets: dict[str, Path]) -> Sandbox:
    """Use the real sandbox package if it provides ``create_sandbox``; otherwise the stub."""
    try:
        from analyst_agent import sandbox as real
    except ImportError:  # pragma: no cover
        real = None
    factory = getattr(real, "create_sandbox", None)
    if callable(factory):
        return factory(datasets)
    return LocalStubSandbox(datasets)
