"""Sandbox interface (per CLAUDE.md).

Exposes ``run_sql(query) -> SqlResult`` and ``run_python(code) -> PyResult``.

THIS IS A STUB, added by feat/agent-core so the agent can run before the real sandbox lands
(feat/sandbox). The two public functions and the two dataclasses (fields and their order) are the
contract the tools rely on; feat/sandbox should keep them and replace the bodies.

``load_data`` is an addition CLAUDE.md does not specify: with module-level ``run_sql`` /
``run_python`` there has to be some way to tell the sandbox which files to expose as tables and
where Python-produced files should be kept.

Errors are returned in the result (``ok=False, error=...``), never raised.

Stub limitations: the SQL side uses an in-memory DuckDB behind a SELECT/WITH/EXPLAIN allowlist and
a 200-row cap. The Python side is a plain subprocess with a timeout and is NOT a security boundary.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["PyResult", "SqlResult", "load_data", "loaded_tables", "run_python", "run_sql"]

ROW_CAP = 200
PREVIEW_ROWS = 50
PYTHON_TIMEOUT_S = 60.0


@dataclass
class SqlResult:
    ok: bool
    rows: list[dict[str, Any]] = field(default_factory=list)  # at most ROW_CAP rows
    columns: list[str] = field(default_factory=list)
    row_count: int = 0  # total rows the query produced, before the cap
    truncated: bool = False
    markdown_preview: str = ""
    error: str | None = None
    duration_ms: float = 0.0


@dataclass
class PyResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    artifacts: list[str] = field(default_factory=list)  # paths of files the code wrote
    error: str | None = None
    duration_ms: float = 0.0


# --------------------------------------------------------------------------- stub state

_datasets: dict[str, Path] = {}
_artifacts_dir: Path = Path("artifacts")
_con: Any = None  # duckdb.DuckDBPyConnection


def load_data(
    datasets: Mapping[str, str | Path], artifacts_dir: str | Path = "artifacts"
) -> list[str]:
    """Replace the loaded data with ``datasets`` (table name -> CSV/Parquet path).

    Files written by ``run_python`` are copied into ``artifacts_dir``.
    """
    import duckdb

    global _con, _artifacts_dir
    con = duckdb.connect(":memory:")
    for name, path in datasets.items():
        reader = "read_parquet" if str(path).lower().endswith(".parquet") else "read_csv_auto"
        ident = '"' + name.replace('"', '""') + '"'
        con.execute(f"CREATE TABLE {ident} AS SELECT * FROM {reader}(?)", [str(path)])
    if _con is not None:
        _con.close()
    _con = con
    _artifacts_dir = Path(artifacts_dir)
    _datasets.clear()
    _datasets.update({k: Path(v) for k, v in datasets.items()})
    return list(_datasets)


def loaded_tables() -> list[str]:
    return list(_datasets)


def _ms_since(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 2)


# --------------------------------------------------------------------------- run_sql

_ALLOWED_SQL = re.compile(r"^\s*(select|with|explain)\b", re.IGNORECASE)


def _markdown_preview(
    columns: list[str], rows: list[dict[str, Any]], row_count: int, truncated: bool
) -> str:
    def cell(v: Any) -> str:
        return "" if v is None else str(v).replace("|", "\\|").replace("\n", " ")

    shown = rows[:PREVIEW_ROWS]
    head = f"{row_count:,} row(s)"
    if len(shown) < row_count:
        head += f"; showing first {len(shown)}"
    if truncated:
        head += f" (result capped at {ROW_CAP} rows)"
    if not columns:
        return head
    lines = [head, "", "| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    lines += ["| " + " | ".join(cell(r.get(c)) for c in columns) + " |" for r in shown]
    return "\n".join(lines)


def _jsonable(v: Any) -> Any:
    return v if v is None or isinstance(v, (bool, int, float, str)) else str(v)


def run_sql(query: str) -> SqlResult:
    t0 = time.perf_counter()
    if _con is None:
        return SqlResult(ok=False, error="No data loaded.", duration_ms=_ms_since(t0))
    q = query.strip().rstrip(";").strip()
    if not _ALLOWED_SQL.match(q):
        return SqlResult(
            ok=False,
            error="Only SELECT / WITH / EXPLAIN statements are allowed.",
            duration_ms=_ms_since(t0),
        )
    if ";" in q:
        return SqlResult(
            ok=False, error="Multiple statements are not allowed.", duration_ms=_ms_since(t0)
        )
    try:
        cur = _con.execute(q)
        columns = [d[0] for d in cur.description or []]
        fetched = cur.fetchmany(ROW_CAP + 1)
        truncated = len(fetched) > ROW_CAP
        row_count = len(fetched)
        if truncated:
            row_count = _con.execute(f"SELECT COUNT(*) FROM ({q}) AS _sub").fetchone()[0]
            fetched = fetched[:ROW_CAP]
        rows = [{c: _jsonable(v) for c, v in zip(columns, r, strict=True)} for r in fetched]
    except Exception as exc:  # noqa: BLE001 - returned as a structured error
        return SqlResult(ok=False, error=f"{type(exc).__name__}: {exc}", duration_ms=_ms_since(t0))
    return SqlResult(
        ok=True,
        rows=rows,
        columns=columns,
        row_count=row_count,
        truncated=truncated,
        markdown_preview=_markdown_preview(columns, rows, row_count, truncated),
        duration_ms=_ms_since(t0),
    )


# --------------------------------------------------------------------------- run_python


def _collect_artifacts(workdir: Path, skip: set[Path]) -> list[str]:
    """Copy files the snippet wrote into the artifacts dir; return their new paths."""
    out: list[str] = []
    for f in sorted(workdir.rglob("*")):
        if not f.is_file() or f in skip:
            continue
        _artifacts_dir.mkdir(parents=True, exist_ok=True)
        dest = _artifacts_dir / f"py-{uuid.uuid4().hex[:8]}-{f.name}"
        shutil.copy2(f, dest)
        out.append(str(dest))
    return out


def run_python(code: str) -> PyResult:
    t0 = time.perf_counter()
    paths = json.dumps({k: str(v) for k, v in _datasets.items()})
    prelude = (
        "import json, pandas as pd, numpy as np\n"
        f"_paths = json.loads({paths!r})\n"
        "tables = {k: (pd.read_parquet(p) if p.endswith('.parquet') else pd.read_csv(p))"
        " for k, p in _paths.items()}\n"
        "df = next(iter(tables.values())) if len(tables) == 1 else None\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        script = workdir / "_snippet.py"
        script.write_text(prelude + code)
        try:
            proc = subprocess.run(
                [sys.executable, "-I", str(script)],
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=PYTHON_TIMEOUT_S,
                env={"PATH": "/usr/bin:/bin", "MPLBACKEND": "Agg"},
            )
        except subprocess.TimeoutExpired:
            return PyResult(
                ok=False,
                error=f"Timed out after {PYTHON_TIMEOUT_S:.0f}s",
                duration_ms=_ms_since(t0),
            )
        artifacts = _collect_artifacts(workdir, skip={script})
    if proc.returncode != 0:
        # Traceback tail: enough for the model to fix its code.
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:]) or "unknown error"
        return PyResult(
            ok=False,
            stdout=proc.stdout,
            stderr=proc.stderr,
            artifacts=artifacts,
            error=tail,
            duration_ms=_ms_since(t0),
        )
    return PyResult(
        ok=True,
        stdout=proc.stdout,
        stderr=proc.stderr,
        artifacts=artifacts,
        duration_ms=_ms_since(t0),
    )
