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
    sql_fields = {f.name for f in dataclasses.fields(SqlResult)}
    assert {"ok", "columns", "rows", "total_rows", "truncated", "error"} <= sql_fields
    assert {"ok", "stdout", "stderr", "error"} <= {f.name for f in dataclasses.fields(PyResult)}


def test_run_sql_and_python(sales_csv: Path):
    assert sandbox.load_data({"sales": sales_csv}) == ["sales"]
    res = sandbox.run_sql("SELECT COUNT(*) AS n FROM sales")
    assert isinstance(res, SqlResult) and res.ok and res.rows == [{"n": 4}]
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
    assert res.ok and len(res.rows) == 200 and res.total_rows == 500 and res.truncated
