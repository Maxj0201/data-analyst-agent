"""``eval run --resume``: continue an interrupted run without paying for finished tasks twice."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from analyst_agent.eval.agent import AgentInput, AgentOutput
from analyst_agent.eval.cli import app
from analyst_agent.eval.runner import (
    PENDING_JUDGE,
    ResumeError,
    RunConfig,
    judge_incomplete,
    load_results,
    resume_plan,
    run_tasks,
)
from tests.eval.conftest import MockJudge, make_task


def _row(tid: str, status: str = "ok", **details: Any) -> dict[str, Any]:
    return {"task_id": tid, "status": status, "score_details": details or {"method": "numeric"}}


def test_resume_plan_classifies_rows() -> None:
    rows = [
        _row("scored"),
        _row("budget", "over_budget", method="not_run", skipped=True),
        _row("err", "error", method="not_run"),
        _row("tmo", "timeout", method="not_run"),
        _row("pending", method=PENDING_JUDGE, skipped=True),
        _row("l3_nojudge", method="rubric_judge", skipped=True, error="no judge configured"),
        _row("l3_apierr", method="rubric_judge", error="judge call failed: 529"),
        _row("l3_ok", method="rubric_judge", points=[], pass_threshold=0.5),
        _row("adv_err", method="adversarial", judge={"passed": False, "error": "boom"}),
        _row("adv_ok", method="adversarial", judge={"passed": True}),
        _row("unscored", method="unscored"),
        # a later row for the same task wins (e.g. the verdict after a pending row)
        _row("later", method=PENDING_JUDGE, skipped=True),
        _row("later", method="rubric_judge", points=[]),
    ]
    ids = [*dict.fromkeys(r["task_id"] for r in rows), "missing"]
    plan = resume_plan(rows, ids)
    assert plan == {
        "scored": "done",
        "budget": "run",
        "err": "done",
        "tmo": "done",
        "pending": "rescore",
        "l3_nojudge": "rescore",
        "l3_apierr": "rescore",
        "l3_ok": "done",
        "adv_err": "rescore",
        "adv_ok": "done",
        "unscored": "done",
        "later": "done",
        "missing": "run",
    }
    retry = resume_plan(rows, ids, retry_errors=True)
    assert retry["err"] == retry["tmo"] == "run"
    no_judge = resume_plan(rows, ids, rescore=False)
    assert no_judge["pending"] == no_judge["l3_apierr"] == "done"
    assert not judge_incomplete(_row("x", "error", method=PENDING_JUDGE))


class CountingAgent:
    def __init__(self, cost: float = 0.4, fail: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self.cost, self.fail = cost, fail or set()

    async def __call__(self, inp: AgentInput) -> AgentOutput:
        self.calls.append(inp.task_id)
        if inp.task_id in self.fail:
            raise RuntimeError("agent crashed")
        return AgentOutput(answer="The answer is 42.", cost_usd=self.cost)


def _tasks() -> list[Any]:
    return [make_task("L1").model_copy(update={"id": f"t{i}"}) for i in range(5)]


async def _run(tmp_path: Path, agent: Any, **kw: Any) -> tuple[Path, list[Any]]:
    cfg = RunConfig(concurrency=1, isolation="inline", budget_usd=kw.pop("budget", None))
    return await run_tasks(
        kw.pop("tasks", _tasks()),
        agent,
        judge=kw.pop("judge", None),
        config=cfg,
        run_dir=tmp_path / "run",
        progress=False,
        **kw,
    )


@pytest.mark.asyncio
async def test_resume_after_the_budget_cap_runs_only_unstarted_tasks(tmp_path: Path) -> None:
    first = CountingAgent()
    run_dir, results = await _run(tmp_path, first, budget=1.0)
    assert first.calls == ["t0", "t1", "t2"]  # 1.2 >= 1.0 after three tasks
    assert [r.status for r in results].count("over_budget") == 2

    second = CountingAgent()
    _, results = await _run(tmp_path, second, budget=5.0, resume=True)
    assert second.calls == ["t3", "t4"]  # nothing already scored is paid for again
    assert [r.status for r in results] == ["ok"] * 5 and all(r.passed for r in results)
    rows = load_results(run_dir)
    assert len(rows) == 5 and all(r["status"] == "ok" for r in rows)
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["spend_usd"] == pytest.approx(2.0)  # 1.2 before + 0.8 now
    assert meta["budget"] == {"usd": 5.0, "exhausted": False, "tasks_not_started": 0}
    (resumed,) = meta["resumes"]
    assert resumed["done"] == 3 and resumed["run"] == 2 and resumed["rescore"] == 0
    assert resumed["prior_spend_usd"] == pytest.approx(1.2)


@pytest.mark.asyncio
async def test_resume_cap_counts_spend_from_before(tmp_path: Path) -> None:
    await _run(tmp_path, CountingAgent(), budget=1.0)
    again = CountingAgent()
    _, results = await _run(tmp_path, again, budget=1.5, resume=True)
    assert again.calls == ["t3"]  # 1.2 + 0.4 = 1.6 >= 1.5: t4 still does not start
    assert [r.status for r in results][-1] == "over_budget"


@pytest.mark.asyncio
async def test_resume_after_a_crash_runs_the_missing_tasks(tmp_path: Path) -> None:
    run_dir, _ = await _run(tmp_path, CountingAgent())
    meta = json.loads((run_dir / "meta.json").read_text())
    # A crash: two rows never written, meta.json never finalised (no spend_usd).
    lines = (run_dir / "results.jsonl").read_text().splitlines()
    (run_dir / "results.jsonl").write_text("\n".join(lines[:3]) + "\n")
    for k in ("finished_at", "spend_usd"):
        meta.pop(k)
    (run_dir / "meta.json").write_text(json.dumps(meta))

    agent = CountingAgent()
    _, results = await _run(tmp_path, agent, resume=True)
    assert agent.calls == ["t3", "t4"] and len(results) == 5
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["spend_usd"] == pytest.approx(2.0)  # prior spend summed from the rows


@pytest.mark.asyncio
async def test_resume_keeps_errors_unless_asked(tmp_path: Path) -> None:
    await _run(tmp_path, CountingAgent(fail={"t2"}))
    agent = CountingAgent()
    _, results = await _run(tmp_path, agent, resume=True)
    assert agent.calls == [] and results[2].status == "error"
    _, results = await _run(tmp_path, agent, resume=True, retry_errors=True)
    assert agent.calls == ["t2"] and results[2].status == "ok"


@pytest.mark.asyncio
async def test_resume_regrades_pending_verdicts_without_rerunning_the_agent(
    tmp_path: Path,
) -> None:
    tasks = [
        make_task("L3").model_copy(update={"id": "l3"}),
        make_task("ADV").model_copy(update={"id": "adv"}),
        make_task("L1").model_copy(update={"id": "l1"}),
    ]
    # The judge failed (e.g. a batch that never came back / an outage) on the first attempt.
    run_dir, results = await _run(
        tmp_path, CountingAgent(), tasks=tasks, judge=MockJudge(fail=True)
    )
    assert not results[0].passed and "error" in results[0].score_details

    agent, judge = CountingAgent(), MockJudge()
    _, results = await _run(tmp_path, agent, tasks=tasks, judge=judge, resume=True)
    assert agent.calls == []  # graded from the saved answer
    assert len(judge.calls) == 2 and "The answer is 42." in judge.calls[0]["user"]
    assert [r.passed for r in results] == [True, True, True]
    assert {r["task_id"]: r["passed"] for r in load_results(run_dir)} == {
        "l3": True,
        "adv": True,
        "l1": True,
    }


@pytest.mark.asyncio
async def test_resume_refuses_different_settings_or_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run(tmp_path, CountingAgent())
    with pytest.raises(ResumeError, match="task list"):
        await _run(tmp_path, CountingAgent(), tasks=_tasks()[:3], resume=True)
    monkeypatch.setenv("AGENT_MODEL", "claude-haiku-5-5")
    with pytest.raises(ResumeError, match="AGENT_MODEL"):
        await _run(tmp_path, CountingAgent(), resume=True)
    with pytest.raises(ResumeError, match="no meta.json"):
        await run_tasks(_tasks(), CountingAgent(), run_dir=tmp_path / "nope", resume=True)


def test_cli_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    runner = CliRunner()
    runs = tmp_path / "runs"
    base = ["run", "--agent", "oracle", "--no-judge", "--isolation", "inline"]
    res = runner.invoke(app, [*base, "--limit", "4", "--runs-dir", str(runs)])
    assert res.exit_code == 0, res.output
    (run_dir,) = runs.iterdir()
    lines = (run_dir / "results.jsonl").read_text().splitlines()
    (run_dir / "results.jsonl").write_text(lines[0] + "\n")  # "crash" after one task

    res = runner.invoke(app, ["run", "--resume", str(run_dir)])  # isolation from meta.json
    assert res.exit_code == 0, res.output
    assert "1 done, 0 to re-score, 3 to run" in res.output
    rows = load_results(run_dir)
    assert len(rows) == 4 and all(r["status"] == "ok" for r in rows)
    assert (run_dir / "report.md").is_file()

    res = runner.invoke(app, ["run", "--resume", str(run_dir), "--limit", "2"])
    assert res.exit_code != 0 and "task filters" in res.output
    res = runner.invoke(app, ["run", "--retry-errors", "--limit", "2"])
    assert res.exit_code != 0 and "--resume" in res.output
    res = runner.invoke(app, ["run", "--resume", str(tmp_path)])
    assert res.exit_code != 0 and "meta.json" in res.output
