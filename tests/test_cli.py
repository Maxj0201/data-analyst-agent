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


# --------------------------------------------------------------------------- flag matrix


@pytest.mark.parametrize("routing", [True, False])
@pytest.mark.parametrize("semantic", [True, False])
@pytest.mark.parametrize("approval", ["auto", "prompt-yes", "prompt-no", "off"])
@pytest.mark.parametrize("structured", [True, False])
@pytest.mark.parametrize("memory", [True, False])
def test_ask_dry_run_every_flag_combination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    routing: bool,
    semantic: bool,
    approval: str,
    structured: bool,
    memory: bool,
):
    import json

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_STRUCTURED_REPORT_ENABLED", str(structured).lower())
    monkeypatch.setenv("AGENT_MEMORY_ENABLED", str(memory).lower())
    monkeypatch.setenv("AGENT_MEMORY_DB_PATH", str(tmp_path / "memory.sqlite3"))
    monkeypatch.setenv("AGENT_ARTIFACTS_DIR", str(tmp_path / "art"))
    monkeypatch.setenv("AGENT_SEMANTIC_METRICS_DIR", str(REPO / "benchmarks/metrics"))
    monkeypatch.setenv("AGENT_APPROVAL_ROW_THRESHOLD", "0")  # gate every SELECT
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    trace = tmp_path / "trace.json"
    args = [
        "ask",
        "--data",
        str(REPO / "benchmarks/data/saas/subscriptions.csv"),
        "which region has the highest mrr",
        "--dry-run",
        "--trace",
        str(trace),
        "--routing" if routing else "--no-routing",
        "--semantic-layer" if semantic else "--no-semantic-layer",
        "--no-require-approval" if approval == "off" else "--require-approval",
    ]
    if approval == "auto":
        args.append("--auto-approve")
    stdin = {"prompt-yes": "y\n" * 5, "prompt-no": "n\n\n" * 5}.get(approval, "")

    res = CliRunner().invoke(app, args, input=stdin)

    assert res.exit_code == 0, res.output
    t = json.loads(trace.read_text())
    assert t["status"] == "completed"
    assert set(t["prompts"]) >= {"planner", "router", "semantic_layer_rules"}
    assert (t["routing"] is not None and t["routing"]["routed_tier"] == "L2") is routing
    sl = t["semantic_layer"]
    assert sl["enabled"] is semantic and bool(sl["retrieved_metrics"]) is semantic
    assert sl["error"] is None
    # Structured reports: the dry run submits the structured fields and the checks pass.
    assert (t["structured_report"] is not None) is structured
    if structured:
        assert t["structured_report"]["key_numbers"]["top_region"] == "North America"
    mem = t["memory"]
    assert mem["enabled"] is memory and mem["error"] is None
    if memory and approval != "prompt-no":
        assert mem["saved"] and mem["fingerprint"]
    decisions = {(a["decision"]["approved"], a["decision"]["decided_by"]) for a in t["approvals"]}
    assert (
        decisions
        == {
            "auto": {(True, "auto")},
            "prompt-yes": {(True, "human")},
            "prompt-no": {(False, "human")},
            "off": set(),
        }[approval]
    )
    if approval != "prompt-no":
        assert "**North America** has the highest total_mrr" in res.output


# --------------------------------------------------------------------------- trace show / ls


def test_trace_show_prints_saved_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import json
    import re

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_ARTIFACTS_DIR", str(tmp_path / "art"))
    monkeypatch.setenv("AGENT_TRACE_STORE_ENABLED", "true")
    monkeypatch.setenv("AGENT_TRACES_DIR", str(tmp_path / "traces"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    data = REPO / "benchmarks/data/saas/subscriptions.csv"
    ask_trace = tmp_path / "ask.json"
    runner = CliRunner()

    res = runner.invoke(
        app,
        ["ask", "-d", str(data), "which region has the highest revenue", "--dry-run",
         "--trace", str(ask_trace)],
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    m = re.search(r"analyst-agent trace show (run_[0-9a-f]+)", res.output)
    assert m, res.output
    run_id = m.group(1)
    assert (tmp_path / "traces" / f"{run_id}.json").is_file()

    shown = runner.invoke(app, ["trace", "show", run_id])
    assert shown.exit_code == 0, shown.output
    out = shown.output
    assert f"✓ run {run_id} status=completed" in out
    assert "├─ ✓ turn 1" in out and "llm:agent dry-run" in out and "tool:run_sql" in out
    assert "critic round=1 verdict=pass" in out
    assert re.search(r"total \d+(ms|\.\d\ds) \| \$0\.0000 \| \d+ LLM calls \| \d+ tool calls", out)
    assert "planner@" in out
    assert "─ ✓ plan " not in runner.invoke(app, ["trace", "show", run_id, "--compact"]).output

    # ``ask --trace`` output carries the same tree, so a path works too.
    by_path = runner.invoke(app, ["trace", "show", str(ask_trace), "--json"])
    assert by_path.exit_code == 0, by_path.output
    tree = json.loads(by_path.output[by_path.output.index("{") :])
    assert tree["kind"] == "run" and tree["metadata"]["run_id"] == run_id

    listed = runner.invoke(app, ["trace", "ls"])
    assert run_id in listed.output and "completed" in listed.output

    missing = runner.invoke(app, ["trace", "show", "run_nope"])
    assert missing.exit_code == 1 and "No saved trace" in missing.output
