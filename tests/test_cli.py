"""``analyst-agent ask --dry-run`` runs the full pipeline offline with the scripted FakeLLM."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from analyst_agent.cli import app

REPO = Path(__file__).resolve().parents[1]


def test_ask_dry_run_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_ARTIFACTS_DIR", str(tmp_path / "art"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    data = REPO / "benchmarks/data/saas/subscriptions.csv"

    res = CliRunner().invoke(
        app, ["ask", "--data", str(data), "which region has the highest revenue", "--dry-run"]
    )

    assert res.exit_code == 0, res.output
    assert "**North America** has the highest total_mrr" in res.output
    assert '"top_region": "North America"' in res.output
    assert list((tmp_path / "art").glob("*.png")), "make_chart should have written a PNG"


def test_ask_help_lists_all_input_formats():
    res = CliRunner().invoke(app, ["ask", "--help"])
    flat = " ".join(res.output.split())
    for fmt in ("CSV", "Parquet", "SQLite", "DuckDB"):
        assert fmt in flat


def test_ask_dry_run_duckdb_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import duckdb

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_ARTIFACTS_DIR", str(tmp_path / "art"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    db = tmp_path / "shop.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE subs AS SELECT * FROM (VALUES ('East', 10.0), ('West', 30.0)) t(region, mrr)"
    )
    con.close()

    res = CliRunner().invoke(
        app, ["ask", "--data", str(db), "which region has the highest mrr", "--dry-run"]
    )

    assert res.exit_code == 0, res.output
    assert "West" in res.output


def test_ask_unsupported_file_type_is_a_usage_error(tmp_path: Path):
    bad = tmp_path / "x.xlsx"
    bad.write_text("nope")
    res = CliRunner().invoke(app, ["ask", "--data", str(bad), "q", "--dry-run"])
    assert res.exit_code == 2
    assert "unsupported file type" in res.output
