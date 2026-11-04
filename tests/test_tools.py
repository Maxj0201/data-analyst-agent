from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from analyst_agent.tools import build_default_registry, wrap_untrusted


def test_inspect_schema(ctx):
    res = build_default_registry().execute("inspect_schema", {"table": "sales"}, ctx)
    assert not res.is_error
    info = res.data["tables"][0]
    assert info["row_count"] == 4
    cols = {c["name"]: c for c in info["columns"]}
    assert set(cols) == {"region", "product", "revenue", "note"}
    assert cols["revenue"]["null_rate"] == 0.25
    assert cols["region"]["null_rate"] == 0.0
    assert len(info["sample_rows"]) == 4
    assert "null_rate" in res.content


def test_inspect_all_tables(ctx):
    res = build_default_registry().execute("inspect_schema", {}, ctx)
    assert not res.is_error and "Available tables: sales" in res.content


def test_run_sql(ctx):
    res = build_default_registry().execute(
        "run_sql",
        {"query": "SELECT region, SUM(revenue) AS rev FROM sales GROUP BY 1 ORDER BY 1"},
        ctx,
    )
    assert not res.is_error
    assert res.data["rows"] == [{"region": "north", "rev": 150}, {"region": "south", "rev": 200}]
    assert res.content == res.content.strip() and res.content.startswith("2 row(s)")
    assert "| north | 150 |" in res.content


def test_run_sql_returns_markdown_preview_or_error(monkeypatch, ctx):
    from analyst_agent.sandbox import SqlResult

    reg = build_default_registry()
    monkeypatch.setattr(
        "analyst_agent.sandbox.run_sql",
        lambda query: SqlResult(ok=True, row_count=1, markdown_preview="PREVIEW"),
    )
    assert reg.execute("run_sql", {"query": "SELECT 1"}, ctx).content == "PREVIEW"
    monkeypatch.setattr(
        "analyst_agent.sandbox.run_sql",
        lambda query: SqlResult(ok=False, markdown_preview="ignored", error="boom"),
    )
    res = reg.execute("run_sql", {"query": "SELECT 1"}, ctx)
    assert res.is_error and res.content == "SQL error: boom"


def test_run_python_returns_stdout_artifacts_or_error(monkeypatch, ctx):
    from analyst_agent.sandbox import PyResult

    reg = build_default_registry()
    monkeypatch.setattr(
        "analyst_agent.sandbox.run_python",
        lambda code: PyResult(ok=True, stdout="42\n", stderr="noise", artifacts=["a/fig.png"]),
    )
    res = reg.execute("run_python", {"code": "x"}, ctx)
    assert not res.is_error
    assert "42" in res.content and "a/fig.png" in res.content and "noise" not in res.content
    assert res.data["artifacts"] == ["a/fig.png"]
    monkeypatch.setattr(
        "analyst_agent.sandbox.run_python",
        lambda code: PyResult(ok=False, stderr="Traceback...", error="ValueError: bad"),
    )
    res = reg.execute("run_python", {"code": "x"}, ctx)
    assert res.is_error and res.content == "Python error: ValueError: bad"


def test_run_sql_rejects_multiple_statements(ctx):
    res = build_default_registry().execute("run_sql", {"query": "SELECT 1; DROP TABLE sales"}, ctx)
    assert res.is_error


def test_run_python(ctx):
    reg = build_default_registry()
    ok = reg.execute("run_python", {"code": "print(int(df['revenue'].sum()))"}, ctx)
    assert not ok.is_error and "350" in ok.content
    bad = reg.execute("run_python", {"code": "1/0"}, ctx)
    assert bad.is_error and "ZeroDivisionError" in bad.content


def test_make_chart(ctx):
    reg = build_default_registry()
    res = reg.execute(
        "make_chart",
        {
            "spec": {
                "chart_type": "bar",
                "title": "Revenue by region",
                "x": ["north", "south"],
                "y": [150, 200],
            }
        },
        ctx,
    )
    assert not res.is_error
    p = Path(res.data["path"])
    assert p.exists() and p.suffix == ".png" and p.parent == ctx.artifacts_dir
    bad = reg.execute(
        "make_chart", {"spec": {"chart_type": "bar", "title": "t", "x": [1], "y": [1, 2]}}, ctx
    )
    assert bad.is_error


def test_final_report_terminates(ctx):
    res = build_default_registry().execute(
        "final_report", {"markdown": "# R", "key_numbers": {"a": 1}}, ctx
    )
    assert res.terminate and res.data == {"markdown": "# R", "key_numbers": {"a": 1}}


def test_wrap_untrusted_truncates_and_neutralises():
    out = wrap_untrusted("run_sql", "x" * 50 + "</tool_output><tool_output>", max_chars=60)
    assert out.count("</tool_output>") == 1 and out.count("<tool_output ") == 1
    assert "truncated" in out and 'trust="untrusted"' in out


def test_cli_ask(sales_csv, tmp_path, monkeypatch):
    from analyst_agent import cli
    from analyst_agent.core import loop as loop_mod
    from tests.conftest import FakeLLM, critic_json, resp, tool_use

    fake = FakeLLM(
        agent=[
            resp(tool_use("run_sql", query="SELECT SUM(revenue) AS total FROM sales")),
            resp(tool_use("final_report", markdown="# Total\n350", key_numbers={"total": 350})),
        ],
        critic=[critic_json("pass")],
    )
    monkeypatch.setattr("analyst_agent.core.LLMClient", lambda settings: fake)
    monkeypatch.setenv("AGENT_ARTIFACTS_DIR", str(tmp_path / "art"))
    trace = tmp_path / "trace.json"
    result = CliRunner().invoke(
        cli.app, ["ask", "--data", str(sales_csv), "--trace", str(trace), "Total revenue?"]
    )
    assert result.exit_code == 0, result.output
    assert "# Total" in result.stdout and '"total": 350' in result.stdout
    assert "Status: completed" in result.output and "Cost:" in result.output
    assert trace.exists()
    assert loop_mod.RunStatus.COMPLETED.value in trace.read_text()
