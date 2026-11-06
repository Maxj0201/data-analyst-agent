"""L3 judge mode: strict (result figures must be in key_numbers) vs lenient (content only).

The mocked judge marks the point satisfied but lists a figure that is missing from key_numbers,
the way the real judge graded Haiku's CAC table in the 2026-11-06 smoke run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from analyst_agent.eval.agent import AgentInput, AgentOutput
from analyst_agent.eval.cli import app
from analyst_agent.eval.report import (
    JudgeModeMismatch,
    check_same_judge_mode,
    judge_mode,
    render_compare,
    render_report,
)
from analyst_agent.eval.runner import RunConfig, load_results, run_tasks
from analyst_agent.eval.scorers import RUBRIC_SYSTEM, RUBRIC_SYSTEM_LENIENT, score_task
from tests.eval.conftest import make_task, out

REPORT = "Paid search CAC rose from $56 to $111 while spend grew from 1,066 to 1,784."


class FigureJudge:
    """Scores every rubric point 1 with verbatim evidence, but lists '1,066' as unmatched."""

    def __init__(self) -> None:
        self.systems: list[str] = []

    async def complete(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.systems.append(system)
        return {
            "points": [
                {
                    "index": 0,
                    "evidence": [REPORT],
                    "unmatched_numbers": ["1,066"],
                    "score": 1,
                    "justification": "right figures; 1,066 is not in key_numbers",
                }
            ]
        }


def _l3_task() -> Any:
    return make_task("L3", rubric=["Finds paid_search CAC rising"], pass_threshold=1.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(("strict", "passed"), [(True, False), (False, True)])
async def test_modes_grade_a_figure_missing_from_key_numbers(strict: bool, passed: bool) -> None:
    judge = FigureJudge()
    s = await score_task(
        _l3_task(), out(REPORT, key_numbers={"cac_h1": 56}), judge, strict_numbers=strict
    )
    assert s.passed is passed and s.score == (0.0 if strict else 1.0)
    assert s.details["judge_mode"] == ("strict" if strict else "lenient")
    assert judge.systems == [RUBRIC_SYSTEM if strict else RUBRIC_SYSTEM_LENIENT]
    point = s.details["points"][0]
    assert ("override" in point) is strict  # strict: "figures not in key_numbers: 1,066"


def test_lenient_prompt_drops_only_the_key_numbers_rule() -> None:
    assert "must also appear in\n   <key_numbers>" in RUBRIC_SYSTEM
    assert "must also appear" not in RUBRIC_SYSTEM_LENIENT
    assert "do not require a figure" in RUBRIC_SYSTEM_LENIENT
    # citations and number accuracy are unchanged
    for rule in ("copy them verbatim", "a wrong number fails the point"):
        assert rule in RUBRIC_SYSTEM and rule in RUBRIC_SYSTEM_LENIENT


async def _run(tmp_path: Path, strict: bool) -> tuple[Path, dict[str, Any]]:
    async def agent(inp: AgentInput) -> AgentOutput:
        return AgentOutput(answer=REPORT, key_numbers={"cac_h1": 56})

    run_dir, _ = await run_tasks(
        [_l3_task()],
        agent,
        judge=FigureJudge(),
        config=RunConfig(isolation="inline", judge_model="mock", judge_strict_numbers=strict),
        run_dir=tmp_path / ("strict" if strict else "lenient"),
        progress=False,
    )
    return run_dir, json.loads((run_dir / "meta.json").read_text())


@pytest.mark.asyncio
async def test_mode_is_recorded_reported_and_never_mixed(tmp_path: Path) -> None:
    strict_dir, strict_meta = await _run(tmp_path, True)
    lenient_dir, lenient_meta = await _run(tmp_path, False)
    assert strict_meta["config"]["judge_strict_numbers"] is True
    assert lenient_meta["config"]["judge_strict_numbers"] is False
    assert strict_meta["settings"]["judge_strict_numbers"] is True  # resume compares this
    assert "rubric_judge_lenient" in lenient_meta["prompts"]
    strict_rows, lenient_rows = load_results(strict_dir), load_results(lenient_dir)
    assert [r["passed"] for r in strict_rows] == [False]
    assert [r["passed"] for r in lenient_rows] == [True]
    assert lenient_rows[0]["score_details"]["judge_mode"] == "lenient"

    assert "judge mode: strict (result figures" in render_report(strict_rows, strict_meta)
    assert "judge mode: lenient (content only)" in render_report(lenient_rows, lenient_meta)
    same = render_compare(strict_rows, strict_rows, "a", "b", strict_meta, strict_meta)
    assert "| Judge mode | strict | strict | |" in same
    with pytest.raises(JudgeModeMismatch, match="different L3 judge modes"):
        render_compare(strict_rows, lenient_rows, "a", "b", strict_meta, lenient_meta)

    res = CliRunner().invoke(app, ["compare", str(strict_dir), str(lenient_dir)])
    assert res.exit_code == 2 and "different L3 judge modes" in res.output


def test_judge_mode_of_old_and_unjudged_runs() -> None:
    assert judge_mode({"config": {"judge_model": "claude-sonnet-5-5"}}) == "strict"  # pre-option
    assert judge_mode({"config": {"judge_model": None, "judge_strict_numbers": False}}) is None
    # an unjudged run can sit next to either mode
    check_same_judge_mode(
        {"a": {"config": {"judge_model": None}}, "b": {"config": {"judge_model": "m"}}}
    )


def test_experiments_summary_refuses_mixed_modes(tmp_path: Path) -> None:
    from analyst_agent.eval.experiments import app as exp_app

    runs = {}
    for arm, strict in (("control", True), ("treatment", False)):
        d = tmp_path / arm
        d.mkdir()
        (d / "results.jsonl").write_text("")
        meta = {"config": {"judge_model": "m", "judge_strict_numbers": strict}}
        (d / "meta.json").write_text(json.dumps(meta))
        runs[arm] = d
    manifest = tmp_path / "manifest.tsv"
    manifest.write_text(
        "experiment\tarm\trole\trun_dir\tconfig\n"
        f"baseline\tsonnet\tcontrol\t{runs['control']}\tc\n"
        f"baseline\thaiku\ttreatment\t{runs['treatment']}\tt\n"
    )
    res = CliRunner().invoke(exp_app, ["summary", str(manifest), "--out", str(tmp_path / "s.md")])
    assert res.exit_code == 2 and "different L3 judge modes" in res.output


def test_cli_records_lenient_judge_and_resume_keeps_the_mode(tmp_path: Path) -> None:
    runner = CliRunner()
    args = ["run", "--agent", "oracle", "--no-judge", "-d", "L1", "--limit", "1"]
    res = runner.invoke(app, [*args, "--lenient-judge", "--runs-dir", str(tmp_path)])
    assert res.exit_code == 0, res.output
    (run_dir,) = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert (
        json.loads((run_dir / "meta.json").read_text())["config"]["judge_strict_numbers"] is False
    )

    # resuming a strict run with an explicit --lenient-judge is refused
    strict = tmp_path / "strict"
    res = runner.invoke(app, [*args, "--runs-dir", str(strict)])
    assert res.exit_code == 0, res.output
    (strict_dir,) = [p for p in strict.iterdir() if p.is_dir()]
    res = runner.invoke(app, ["run", "--resume", str(strict_dir), "--lenient-judge"])
    assert res.exit_code == 2 and "the run was graded strict" in res.output
