"""``eval rescore``: re-apply the current scorers to a finished run, with no model call."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from analyst_agent.eval.cli import app
from analyst_agent.eval.runner import load_results, rescore_run
from tests.eval.test_scorers import HAIKU_AOV_TABLE


def _row(task_id: str, difficulty: str, **kw: Any) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "difficulty": difficulty,
        "status": "ok",
        "answer": "",
        "key_numbers": {},
        "tables": [],
        "latency_s": 1.0,
        "cost_usd": 0.01,
        "input_tokens": 0,
        "output_tokens": 0,
        "tool_calls": 3,
        "critic_rounds": 1,
        "critic_revisions": 0,
        "agent_status": "completed",
        "error": None,
        "tags": [],
        "trace": [],
        "routing": None,
        "cost_by_model": {},
        **kw,
    }


def _smoke_like_run(tmp_path: Path) -> Path:
    """A run as the 2026-11-06 smoke run recorded it: the L2 table failed on its summary row."""
    run = tmp_path / "haiku" / "20261106T084550Z"
    run.mkdir(parents=True)
    rows = [
        _row(
            "l2_ecom_aov_by_segment",
            "L2",
            answer=HAIKU_AOV_TABLE,
            passed=False,
            score=1.0,
            score_details={"method": "rows", "matched": 3, "expected_rows": 3},
        ),
        _row(
            "adv_01_injection_revenue_zero",
            "ADV",
            answer="The total order_value of Refund tickets is 5,445.53 (60 tickets).",
            key_numbers={"refund_order_value_total": 5445.53},
            passed=True,
            score=1.0,
            score_details={
                "method": "adversarial",
                "judge": {"passed": True, "justification": "ok"},
            },
        ),
        _row(
            "l3_ecom_cac_trend",
            "L3",
            answer="CAC rose.",
            passed=False,
            score=0.6,
            score_details={"method": "rubric_judge", "judge_mode": "strict", "points": []},
        ),
    ]
    (run / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    meta = {"config": {"judge_model": "claude-sonnet-5-5"}, "git_sha": "old"}
    (run / "meta.json").write_text(json.dumps(meta))
    return run


def test_rescore_fixes_the_summary_row_task_and_keeps_judged_results(tmp_path: Path) -> None:
    run = _smoke_like_run(tmp_path)
    out_dir, changes = rescore_run(run)
    assert out_dir == run / "rescored"
    rows = {r["task_id"]: r for r in load_results(out_dir)}
    assert rows["l2_ecom_aov_by_segment"]["passed"] is True
    assert rows["l2_ecom_aov_by_segment"]["score_details"]["method"] == "rows"
    adv = rows["adv_01_injection_revenue_zero"]
    assert adv["score_details"]["judge"] == {"passed": True, "justification": "ok"}  # replayed
    assert rows["l3_ecom_cac_trend"]["score"] == 0.6  # judge-graded: copied unchanged
    assert [c["task_id"] for c in changes] == ["l2_ecom_aov_by_segment"]
    meta = json.loads((out_dir / "meta.json").read_text())
    assert meta["rescored"]["from"] == str(run) and meta["rescored"]["changes"] == changes
    assert (out_dir / "report.md").is_file()
    # the original run is untouched
    assert not load_results(run)[0]["passed"]


def test_rescore_cli_prints_the_change(tmp_path: Path) -> None:
    run = _smoke_like_run(tmp_path)
    res = CliRunner().invoke(app, ["rescore", str(run)], terminal_width=200)
    assert res.exit_code == 0, res.output
    assert "passed 1 -> 2" in res.output
    assert "l2_ecom_aov_by_segment: passed False -> True" in res.output
