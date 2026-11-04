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


# --------------------------------------------------------------------------- approval


def _gated_ask(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *extra: str, input: str = ""):
    import json

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_ARTIFACTS_DIR", str(tmp_path / "art"))
    monkeypatch.setenv("AGENT_APPROVAL_ROW_THRESHOLD", "0")  # any SELECT needs approval
    monkeypatch.delenv("AGENT_APPROVAL_ENABLED", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    trace = tmp_path / "trace.json"
    data = REPO / "benchmarks/data/saas/subscriptions.csv"
    res = CliRunner().invoke(
        app,
        [
            "ask",
            "--data",
            str(data),
            "which region has the highest revenue",
            "--dry-run",
            "--trace",
            str(trace),
            *extra,
        ],
        input=input,
    )
    return res, json.loads(trace.read_text())


def test_ask_approval_off_by_default(tmp_path, monkeypatch):
    res, trace = _gated_ask(tmp_path, monkeypatch)
    assert res.exit_code == 0, res.output
    assert trace["approvals"] == []


def test_ask_auto_approve(tmp_path, monkeypatch):
    res, trace = _gated_ask(tmp_path, monkeypatch, "--require-approval", "--auto-approve")
    assert res.exit_code == 0, res.output
    assert "Approval required" not in res.output
    assert trace["approvals"]
    assert {a["decision"]["decided_by"] for a in trace["approvals"]} == {"auto"}
    assert all(a["decision"]["approved"] for a in trace["approvals"])


def test_ask_interactive_yes(tmp_path, monkeypatch):
    res, trace = _gated_ask(tmp_path, monkeypatch, "--require-approval", input="y\n" * 5)
    assert res.exit_code == 0, res.output
    assert "Approval required" in res.output and "Run this step?" in res.output
    assert trace["approvals"][0]["action"]["tool"] == "run_sql"
    decision = trace["approvals"][0]["decision"]
    assert decision["approved"] is True and decision["decided_by"] == "human"


def test_ask_interactive_no_with_note(tmp_path, monkeypatch):
    res, trace = _gated_ask(tmp_path, monkeypatch, "--require-approval", input="n\ntoo slow\n" * 5)
    first = trace["approvals"][0]["decision"]
    assert first["approved"] is False and first["note"] == "too slow"
    sql = next(c for c in trace["tool_calls"] if c["name"] == "run_sql")
    assert sql["is_error"] and "too slow" in sql["output"]


def test_ask_no_stdin_denies(tmp_path, monkeypatch):
    res, trace = _gated_ask(tmp_path, monkeypatch, "--require-approval", input="")
    first = trace["approvals"][0]["decision"]
    assert first["approved"] is False and first["note"] == "no answer on stdin"
