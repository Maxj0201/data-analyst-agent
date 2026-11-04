"""Pins the CLAUDE.md interface: sandbox/__init__.py exposes run_sql(query) -> SqlResult and
run_python(code) -> PyResult as dataclasses. The real sandbox (feat/sandbox) must keep passing."""

from __future__ import annotations

import dataclasses
import inspect
from pathlib import Path

from analyst_agent import sandbox
from analyst_agent.sandbox import PyResult, SqlResult


def test_interface_shape():
    assert dataclasses.is_dataclass(SqlResult) and dataclasses.is_dataclass(PyResult)
    assert list(inspect.signature(sandbox.run_sql).parameters) == ["query"]
    assert list(inspect.signature(sandbox.run_python).parameters) == ["code"]
    # Field names, order and annotations are the contract; duration_ms is an int (feat/sandbox).
    assert [(f.name, f.type) for f in dataclasses.fields(SqlResult)] == [
        ("ok", "bool"),
        ("rows", "list[dict[str, Any]]"),
        ("columns", "list[str]"),
        ("row_count", "int"),
        ("truncated", "bool"),
        ("markdown_preview", "str"),
        ("error", "str | None"),
        ("duration_ms", "int"),
    ]
    assert [(f.name, f.type) for f in dataclasses.fields(PyResult)] == [
        ("ok", "bool"),
        ("stdout", "str"),
        ("stderr", "str"),
        ("artifacts", "list[str]"),
        ("error", "str | None"),
        ("duration_ms", "int"),
    ]


def test_run_sql_and_python(sales_csv: Path):
    assert sandbox.load_data({"sales": sales_csv}) == ["sales"]
    res = sandbox.run_sql("SELECT COUNT(*) AS n FROM sales")
    assert isinstance(res, SqlResult) and res.ok and res.rows == [{"n": 4}]
    assert res.row_count == 1 and "| n |" in res.markdown_preview
    assert isinstance(res.duration_ms, int) and res.duration_ms >= 0
    py = sandbox.run_python("print(len(df))")
    assert isinstance(py, PyResult) and py.ok and py.stdout.strip() == "4"


def test_errors_are_returned_not_raised(sales_csv: Path):
    sandbox.load_data({"sales": sales_csv})
    for q in ("DROP TABLE sales", "DELETE FROM sales", "SELECT 1; DROP TABLE sales"):
        res = sandbox.run_sql(q)
        assert not res.ok and res.error
    assert sandbox.run_sql("SELECT COUNT(*) AS n FROM sales").rows == [{"n": 4}]
    py = sandbox.run_python("raise ValueError('boom')")
    assert not py.ok and "boom" in (py.error or "")


def test_row_cap(tmp_path: Path):
    p = tmp_path / "big.csv"
    p.write_text("i\n" + "\n".join(str(i) for i in range(500)))
    sandbox.load_data({"big": p})
    res = sandbox.run_sql("SELECT * FROM big")
    assert res.ok and len(res.rows) == 200 and res.row_count == 500 and res.truncated


def test_python_artifacts(sales_csv: Path, tmp_path: Path):
    art = tmp_path / "art"
    sandbox.load_data({"sales": sales_csv}, artifacts_dir=art)
    py = sandbox.run_python("open('out.txt', 'w').write('hi'); print('done')")
    assert py.ok and py.stdout.strip() == "done"
    assert len(py.artifacts) == 1 and Path(py.artifacts[0]).parent == art
    assert Path(py.artifacts[0]).read_text() == "hi"
