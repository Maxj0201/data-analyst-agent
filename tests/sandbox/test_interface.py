"""The cross-package contract: field names/types and the entry points other packages use."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from analyst_agent import sandbox
from analyst_agent.sandbox import PyResult, SqlResult, create_sandbox
from analyst_agent.sandbox._errors import kind_of


def test_dataclass_fields_are_exactly_the_contract():
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


def test_create_sandbox_sql_and_python(data_files):
    with create_sandbox(data_files) as sb:
        # the query analyst_agent.tools.inspect_schema issues
        cols = sb.run_sql(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'orders' ORDER BY ordinal_position"
        )
        assert cols.ok, cols.error
        assert [r["column_name"] for r in cols.rows] == ["id", "customer", "amount", "note"]
        assert isinstance(cols.duration_ms, int)

        py = sb.run_python(
            "t = tables['orders']\nassert df is t and orders is t\nprint(round(t.amount.sum(), 2))"
        )
        assert py.ok, py.error + py.stderr
        assert py.stdout.strip() == "140.74"
        assert py.error is None and py.artifacts == []


def test_python_artifacts_exported(data_files, _artifacts_to_tmp: Path):
    with create_sandbox(data_files) as sb:
        res = sb.run_python(
            "df.amount.plot.bar()\n"  # left open -> saved by the sandbox
            "fig2 = plt.figure(); plt.plot([1, 2]); plt.savefig('trend.png'); plt.close(fig2)\n"
            "df.describe().to_csv('summary.csv')\n"
            "open('page.html', 'w').write('<script>alert(1)</script>')\n"  # not exported
        )
    assert res.ok, res.error
    names = [Path(p).name for p in res.artifacts]
    assert len(names) == 3, names
    assert any(n.endswith("_trend.png") for n in names)
    assert any(n.endswith("_summary.csv") for n in names)
    assert not any(n.endswith(".html") for n in names)
    for p in res.artifacts:
        assert Path(p).parent == _artifacts_to_tmp and Path(p).stat().st_size > 0
    png = next(p for p in res.artifacts if "open_" in p)
    assert Path(png).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_python_error_is_structured_with_traceback():
    res = sandbox.python_runner.run_python("x = 1\ny = x / 0\n", timeout_s=30)
    assert not res.ok
    assert res.error == "ZeroDivisionError: division by zero"
    assert "line 2" in res.stderr and "y = x / 0" in res.stderr


def test_sqlite_tables_available_in_python(sqlite_file):
    with create_sandbox({"shop": sqlite_file}) as sb:
        assert set(sb.tables) == {"orders", "customers"}
        res = sb.run_python("print(sorted(tables), len(tables['customers']))")
        assert res.ok, res.error
        assert res.stdout.strip() == "['customers', 'orders'] 2"


def test_module_level_run_python_uses_loaded_data(data_files):
    sandbox.reset()
    try:
        sandbox.load_data(data_files)
        res = sandbox.run_python("print(len(df))")
        assert res.ok and res.stdout.strip() == "5"
    finally:
        sandbox.reset()


def test_errors_never_raise(data_files):
    with create_sandbox(data_files) as sb:
        assert kind_of(sb.run_sql("DROP TABLE orders").error) == "StatementNotAllowed"
        assert kind_of(sb.run_python("raise ValueError('boom')").error) == "ValueError"


def test_load_data_artifacts_dir_argument(data_files, tmp_path):
    """agent-core calls ``sandbox.load_data(datasets, artifacts_dir=settings.artifacts_dir)``."""
    target = tmp_path / "run42" / "artifacts"
    sandbox.reset()
    try:
        assert sandbox.load_data(data_files, artifacts_dir=target) == ["orders"]
        assert sandbox.loaded_tables() == ["orders"]
        res = sandbox.run_python("df.amount.plot()\ndf.to_csv('out.csv', index=False)")
        assert res.ok, res.error
        assert len(res.artifacts) == 2
        assert all(Path(p).parent == target and Path(p).is_file() for p in res.artifacts)
        # positional form, and switching the directory on reload
        other = tmp_path / "other"
        sandbox.load_data(data_files, str(other))
        res = sandbox.run_python("open('note.txt', 'w').write('hi')")
        assert [Path(p).parent for p in res.artifacts] == [other]
    finally:
        sandbox.reset()
    assert sandbox.loaded_tables() == []


def test_create_sandbox_artifacts_dir(data_files, tmp_path):
    with create_sandbox(data_files, artifacts_dir=tmp_path / "a") as sb:
        res = sb.run_python("plt.plot([1, 2])")
    assert res.ok and [Path(p).parent for p in res.artifacts] == [tmp_path / "a"]
