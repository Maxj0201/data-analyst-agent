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
