from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from analyst_agent.eval.cli import app
from analyst_agent.eval.report import percentile, render_compare, render_report, summarize


def r(task_id: str, difficulty: str, passed: bool, **kw: Any) -> dict[str, Any]:
    base = {
        "task_id": task_id,
        "difficulty": difficulty,
        "passed": passed,
        "score": 1.0 if passed else 0.0,
        "status": "ok",
        "latency_s": 1.0,
        "cost_usd": 0.1,
        "tool_calls": 2,
        "critic_rounds": 1,
        "critic_revisions": 0,
    }
    return {**base, **kw}


RUN_A = [
    r("a", "L1", True, latency_s=1.0),
    r("b", "L1", False, latency_s=2.0, status="timeout"),
    r("c", "L2", True, latency_s=3.0, critic_revisions=1),
    r("d", "ADV", False, latency_s=4.0, critic_revisions=1),
    r("e", "ADV", True, latency_s=10.0, cost_usd=0.5),
]


def test_percentile() -> None:
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([5], 95) == 5
    assert math.isnan(percentile([], 50))


def test_summarize_metrics() -> None:
    s = summarize(RUN_A)
    assert s["n"] == 5 and s["accuracy"] == 0.6
    assert s["by_difficulty"]["L1"]["accuracy"] == 0.5
    assert s["by_difficulty"]["L1"]["timeouts"] == 1
    assert s["adversarial_pass_rate"] == 0.5
    assert math.isclose(s["mean_cost_usd"], 0.18)
    assert s["latency_p50_s"] == 3.0
    assert math.isclose(s["latency_p95_s"], 8.8)
    assert s["critic_intervention_rate"] == 0.4
    assert s["critic_correction_rate"] == 0.5
    assert "L3" not in s["by_difficulty"]


def test_text_fallback_rate() -> None:
    rows = [
        r("a", "L1", True, score_details={"method": "numeric", "path": "key_numbers"}),
        r("b", "L1", True, score_details={"method": "numeric", "path": "text_fallback"}),
        r("c", "L2", True, score_details={"method": "rows"}),
        r(
            "d",
            "ADV",
            True,
            score_details={
                "method": "adversarial",
                "answer": {"method": "numeric", "details": {"path": "text_fallback"}},
            },
        ),
    ]
    s = summarize(rows)
    assert s["numeric_tasks"] == 3
    assert math.isclose(s["text_fallback_rate"], 2 / 3)
    assert "| Numeric text-fallback rate | 66.7% |" in render_report(rows)
    assert summarize(RUN_A)["text_fallback_rate"] is None


def test_render_report_markdown() -> None:
    md = render_report(RUN_A, {"config": {"agent": "x"}, "git_sha": "abc"})
    assert "| Accuracy (scored) | 60.0% |" in md
    assert "| Adversarial pass rate | 50.0% |" in md
    assert "| L1 | 2 | 1 | 0 | 50.0% |" in md
    assert "| Critic correction rate | 50.0% |" in md


def test_render_compare_flips() -> None:
    run_b = [
        r("a", "L1", False),
        r("b", "L1", True),
        r("c", "L2", True),
        r("d", "ADV", True),
        r("f", "L3", True),
    ]
    md = render_compare(RUN_A, run_b, "base", "new")
    assert "Newly passing in new (2): `b`, `d`" in md
    assert "Regressions in new (1): `a`" in md
    assert "Only in base: 1; only in new: 1" in md
    assert "| Accuracy (scored) | 60.0% | 80.0% | +20.0 pp |" in md


def test_cli_run_report_compare(tmp_path: Path) -> None:
    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "run",
            "--agent",
            "oracle",
            "--no-judge",
            "-d",
            "L1",
            "-d",
            "L2",
            "--limit",
            "4",
            "--runs-dir",
            str(tmp_path),
        ],
    )
    assert res.exit_code == 0, res.output
    run_dirs = sorted(p for p in tmp_path.iterdir() if p.is_dir())
    assert len(run_dirs) == 1
    d = run_dirs[0]
    rows = [json.loads(x) for x in (d / "results.jsonl").read_text().splitlines()]
    assert len(rows) == 4 and all(x["passed"] for x in rows)
    assert (d / "report.md").exists()

    res = runner.invoke(app, ["report", str(d)])
    assert res.exit_code == 0 and "100.0%" in res.output
    res = runner.invoke(app, ["compare", str(d), str(d), "--out", str(tmp_path / "c.md")])
    assert res.exit_code == 0 and (tmp_path / "c.md").exists()
    res = runner.invoke(app, ["list", "-d", "ADV"])
    assert res.exit_code == 0 and res.output.count("adv_") == 10


def test_skipped_tasks_are_excluded_from_accuracy() -> None:
    rows = [
        r("a", "L1", True),
        r("l3", "L3", False, score_details={"method": "rubric_judge", "skipped": True}),
    ]
    s = summarize(rows)
    assert s["n"] == 2 and s["skipped"] == 1 and s["accuracy"] == 1.0
    assert s["by_difficulty"]["L3"]["accuracy"] is None
    md = render_report(rows)
    assert "| Skipped (not gradable) | 1 |" in md and "| L3 | 1 | 0 | 1 | – | – |" in md


def _prompts(**versions: tuple[str, str]) -> dict[str, Any]:
    return {"prompts": {k: {"version": v, "sha256": h} for k, (v, h) in versions.items()}}


def test_compare_shows_prompt_version_diffs() -> None:
    meta_a = _prompts(planner=("1.0.0", "aaa"), critic=("1.0.0", "ccc"), report_writer=("1", "w"))
    meta_b = _prompts(planner=("1.1.0", "bbb"), critic=("1.0.0", "ddd"), report_writer=("1", "w"))
    md = render_compare(RUN_A, RUN_A, "base", "new", meta_a, meta_b)
    assert "## Prompt versions" in md
    assert "| planner | 1.0.0 (`aaa`) | 1.1.0 (`bbb`) | changed |" in md
    assert (
        "| critic | 1.0.0 (`ccc`) | 1.0.0 (`ddd`) | ⚠️ text changed without a version bump |" in md
    )
    assert "report_writer" not in md

    same = render_compare(RUN_A, RUN_A, "a", "b", meta_a, meta_a)
    assert "Identical prompts in both runs." in same
    old = render_compare(RUN_A, RUN_A, "a", "b", {}, meta_a)
    assert "No prompt metadata for a" in old


def test_cli_gate_and_prompt_metadata(tmp_path: Path) -> None:
    runner = CliRunner()
    base = ["run", "--no-judge", "--limit", "5", "--fail-under", "1.0", "--runs-dir"]
    res = runner.invoke(app, [*base, str(tmp_path / "ok"), "--agent", "oracle"])
    assert res.exit_code == 0, res.output
    (run_dir,) = (tmp_path / "ok").iterdir()
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["prompts"]["planner"]["version"] and meta["config"]["isolation"] == "process"
    assert "- prompts: critic@" in (run_dir / "report.md").read_text()

    res = runner.invoke(app, [*base, str(tmp_path / "bad"), "--agent", "tests.eval.agents:echo_42"])
    assert res.exit_code == 1 and "FAILED: accuracy" in res.output

    res = runner.invoke(
        app,
        ["run", "--limit", "1", "--no-score", "--fail-on-error", "--runs-dir", str(tmp_path / "e")]
        + ["--agent", "tests.eval.agents:raising_agent"],
    )
    assert res.exit_code == 1 and "did not complete" in res.output
