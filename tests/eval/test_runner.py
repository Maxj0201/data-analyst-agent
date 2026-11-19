from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import pytest

from analyst_agent.eval.agent import (
    AgentInput,
    AgentOutput,
    OracleAgent,
    load_agent,
    output_from_core,
)
from analyst_agent.eval.runner import RunConfig, load_results, run_task, run_tasks
from analyst_agent.eval.schema import load_tasks
from tests.eval.conftest import MockJudge, echo_agent, make_task


@pytest.mark.asyncio
async def test_run_task_ok_and_scored() -> None:
    res = await run_task(make_task("L1"), echo_agent(), None, timeout_s=5, isolation="inline")
    assert res.status == "ok" and res.passed and res.cost_usd == 0.01
    assert res.score_details["method"] == "numeric"


@pytest.mark.asyncio
async def test_sync_agent_supported() -> None:
    def agent(inp: AgentInput) -> AgentOutput:
        return AgentOutput(
            answer="42",
            trace=[{"type": "tool_call", "name": "run_sql", "input": {"query": "SELECT 42"}}],
        )

    res = await run_task(make_task("L1"), agent, None, timeout_s=5, isolation="inline")
    assert res.passed and res.tool_calls == 1


@pytest.mark.asyncio
async def test_timeout_and_error_are_recorded_not_raised() -> None:
    async def slow(inp: AgentInput) -> AgentOutput:
        await asyncio.sleep(5)
        return AgentOutput()

    async def broken(inp: AgentInput) -> AgentOutput:
        raise ValueError("kaput")

    t0 = time.perf_counter()
    res = await run_task(make_task("L1"), slow, None, timeout_s=0.05, isolation="inline")
    assert res.status == "timeout" and not res.passed and time.perf_counter() - t0 < 2
    res = await run_task(make_task("L1"), broken, None, timeout_s=5, isolation="inline")
    assert res.status == "error" and "kaput" in res.error


@pytest.mark.asyncio
async def test_agent_reported_error_status() -> None:
    async def agent(inp: AgentInput) -> AgentOutput:
        return AgentOutput(status="error", error="API unreachable")

    res = await run_task(make_task("L1"), agent, None, timeout_s=5, isolation="inline")
    assert res.status == "error" and res.error == "API unreachable"


@pytest.mark.asyncio
async def test_run_tasks_concurrency_and_jsonl(tmp_path: Path) -> None:
    active = peak = 0

    async def agent(inp: AgentInput) -> AgentOutput:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return AgentOutput(
            answer="42",
            cost_usd=0.5,
            critic_rounds=1,
            critic_revisions=1,
            trace=[{"type": "critic", "verdict": "revise"}],
        )

    tasks = [make_task("L1").model_copy(update={"id": f"t{i}"}) for i in range(7)]
    run_dir, results = await run_tasks(
        tasks,
        agent,
        config=RunConfig(concurrency=2, isolation="inline"),
        run_dir=tmp_path / "run",
        progress=False,
    )
    assert peak == 2
    assert [r.task_id for r in results] == [f"t{i}" for i in range(7)]
    rows = load_results(run_dir)
    assert len(rows) == 7
    for key in ["trace", "cost_usd", "latency_s", "tool_calls", "critic_rounds", "passed"]:
        assert key in rows[0]
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["n_tasks"] == 7 and "finished_at" in meta


@pytest.mark.asyncio
async def test_budget_usd_stops_starting_tasks(tmp_path: Path) -> None:
    from analyst_agent.eval.report import render_report, summarize

    started: list[str] = []

    async def agent(inp: AgentInput) -> AgentOutput:
        started.append(inp.task_id)
        return AgentOutput(answer="42", cost_usd=0.4)

    class CostlyJudge(MockJudge):
        cost = 0.25  # judge spend counts towards the cap too

    tasks = [make_task("L1").model_copy(update={"id": f"t{i}"}) for i in range(5)]
    run_dir, results = await run_tasks(
        tasks,
        agent,
        judge=CostlyJudge(),
        config=RunConfig(concurrency=1, isolation="inline", budget_usd=1.0),
        run_dir=tmp_path / "run",
        progress=False,
    )
    # 0.25 (judge) + 0.4 + 0.4 = 1.05 >= 1.0 after two tasks: the other three never start.
    assert started == ["t0", "t1"]
    assert [r.status for r in results] == ["ok", "ok"] + ["over_budget"] * 3
    assert all(r.score_details["skipped"] for r in results[2:])
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["config"]["budget_usd"] == 1.0
    assert meta["budget"] == {"usd": 1.0, "exhausted": True, "tasks_not_started": 3}
    assert meta["spend_usd"] == pytest.approx(1.05)
    rows = load_results(run_dir)
    s = summarize(rows)
    assert s["over_budget"] == 3 and s["skipped"] == 3 and s["accuracy"] == 1.0
    assert "| Not started (--budget-usd) | 3 |" in render_report(rows)


@pytest.mark.asyncio
async def test_no_budget_runs_everything(tmp_path: Path) -> None:
    tasks = [make_task("L1").model_copy(update={"id": f"t{i}"}) for i in range(3)]
    run_dir, results = await run_tasks(
        tasks,
        echo_agent(),
        config=RunConfig(isolation="inline"),
        run_dir=tmp_path / "run",
        progress=False,
    )
    assert all(r.status == "ok" for r in results)
    assert "budget" not in json.loads((run_dir / "meta.json").read_text())


@pytest.mark.asyncio
async def test_no_score_mode(tmp_path: Path) -> None:
    judge = MockJudge()
    _, results = await run_tasks(
        [make_task("L3")],
        echo_agent(),
        judge=judge,
        config=RunConfig(score=False, isolation="inline"),
        run_dir=tmp_path,
        progress=False,
    )
    assert results[0].score_details["method"] == "unscored" and not judge.calls


@pytest.mark.asyncio
async def test_oracle_scores_full_marks_on_starter_benchmark(tmp_path: Path) -> None:
    """End-to-end harness check: ground-truth answers must pass every scorer."""
    tasks = load_tasks()
    judge = MockJudge()
    _, results = await run_tasks(
        tasks,
        OracleAgent(tasks),
        judge=judge,
        config=RunConfig(concurrency=8),
        run_dir=tmp_path,
        progress=False,
    )
    failed = [(r.task_id, r.score_details) for r in results if not r.passed]
    assert not failed
    # judge used exactly for L3 + ADV
    assert len(judge.calls) == sum(t.difficulty in ("L3", "ADV") for t in tasks)


@pytest.mark.asyncio
async def test_wrong_agent_fails_l1_l2(tmp_path: Path) -> None:
    tasks = load_tasks(difficulties=["L1", "L2"])
    _, results = await run_tasks(
        tasks,
        echo_agent("I don't know."),
        config=RunConfig(isolation="inline"),
        run_dir=tmp_path,
        progress=False,
    )
    assert not any(r.passed for r in results)


def test_load_agent_specs() -> None:
    assert isinstance(load_agent("oracle", []), OracleAgent)
    fn = load_agent("tests.eval.conftest:echo_agent")
    assert callable(fn)
    with pytest.raises(ValueError):
        load_agent("nope")


# ------------------------------------------------- adapter for core.loop.AgentResult (faked)


class FakeStatus(StrEnum):
    COMPLETED_WITH_ISSUES = "completed_with_issues"
    ERROR = "error"


@dataclass
class FakeCost:
    usd: float = 0.12
    input_tokens: int = 1000
    output_tokens: int = 200
    cache_creation_input_tokens: int = 50
    cache_read_input_tokens: int = 500


@dataclass
class FakeRecord:
    id: str
    name: str
    input: dict[str, Any]
    output: str
    is_error: bool
    turn: int
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeState:
    plan: list[str]
    tool_results: list[FakeRecord]
    critic_reviews: list[dict[str, Any]]
    critic_rounds: int
    cost: FakeCost
    detail: str = ""


@dataclass
class FakeResult:
    status: FakeStatus
    report: str
    key_numbers: dict[str, Any]
    state: FakeState


def test_output_from_core_maps_agent_result() -> None:
    state = FakeState(
        plan=["1. count"],
        tool_results=[
            FakeRecord("a", "run_sql", {"query": "SELECT 1"}, "x" * 10_000, False, 1),
            FakeRecord("b", "final_report", {"markdown": "r"}, "ok", False, 2),
        ],
        critic_reviews=[{"verdict": "revise", "issues": ["wrong"]}, {"verdict": "pass"}],
        critic_rounds=2,
        cost=FakeCost(),
    )
    out = output_from_core(FakeResult(FakeStatus.COMPLETED_WITH_ISSUES, "report", {"n": 1}, state))
    assert out.answer == "report" and out.key_numbers == {"n": 1}
    assert out.tool_calls == 2 and out.critic_rounds == 2 and out.critic_revisions == 1
    assert out.cost_usd == 0.12 and out.input_tokens == 1550 and out.output_tokens == 200
    assert out.status == "completed_with_issues" and out.error is None
    assert len(out.trace[1]["output"]) == 4000
    assert [e["type"] for e in out.trace] == ["plan", "tool_call", "tool_call", "critic", "critic"]
    json.dumps(out.trace)

    err = output_from_core(
        FakeResult(FakeStatus.ERROR, "", {}, FakeState([], [], [], 0, FakeCost(), detail="boom"))
    )
    assert err.status == "error" and err.error == "boom"


def test_progress_log_folds_results_and_survives_a_torn_line(tmp_path) -> None:
    from analyst_agent.eval.agent import ProgressLog, progress_event

    log = ProgressLog(tmp_path / "p.jsonl")
    for kind, data in [
        ("plan", {"plan": "look", "turn": 1}),
        ("tool_call", {"name": "run_sql", "input": {"query": "SELECT 1"}, "turn": 1}),
        ("tool_result", {"name": "run_sql", "is_error": False, "summary": "x=1"}),
        ("llm_call", {"usd": 0.1}),  # not journalled
        ("tool_call", {"name": "run_python", "input": {"code": "while True: pass"}, "turn": 2}),
    ]:
        ev = progress_event(kind, data)
        if ev is not None:
            log.add(ev)
    with open(log.path, "a") as f:
        f.write('{"type": "tool_ca')  # the process was killed mid-write
    trace = log.trace()
    assert [e["type"] for e in trace] == ["plan", "tool_call", "tool_call"]
    assert trace[1]["output"] == "x=1" and trace[1]["is_error"] is False
    assert "output" not in trace[2]  # still running when it was killed


@pytest.mark.asyncio
async def test_host_sleep_between_task_starts_is_flagged_in_meta(tmp_path, monkeypatch) -> None:
    """The 2026-11-14 semantic arms ran through a laptop's idle sleep without a trace of it in
    meta.json. Simulate a 40-minute suspend: the wall clock jumps, the monotonic clock doesn't."""
    from analyst_agent.eval import runner

    clock = {"wall": 1_000_000.0, "mono": 0.0, "calls": 0}

    def wall() -> float:
        clock["calls"] += 1
        if clock["calls"] == 2:  # between the first and second task start
            clock["wall"] += 40 * 60
        clock["wall"] += 1
        return clock["wall"]

    def mono() -> float:
        clock["mono"] += 1
        return clock["mono"]

    monkeypatch.setattr(runner, "_wall_clock", wall)
    monkeypatch.setattr(runner, "_mono_clock", mono)
    tasks = [make_task("L1").model_copy(update={"id": f"t{i}"}) for i in range(3)]
    run_dir, _ = await run_tasks(
        tasks,
        echo_agent(),
        config=RunConfig(concurrency=1, isolation="inline", score=False),
        run_dir=tmp_path / "run",
        progress=False,
    )
    meta = json.loads((run_dir / "meta.json").read_text())
    (ev,) = meta["host_sleep"]
    assert ev["after"] == "start t0" and ev["before"] == "start t1"
    assert ev["suspended_s"] == pytest.approx(2400, abs=5)
    assert meta["warnings"] == [
        "host slept 1x (~40 min) during the run: timeouts and latency "
        "of tasks in flight are unreliable"
    ]


def test_host_sleep_detector_ignores_long_tasks() -> None:
    """A long gap between task starts with no suspend (busy slots) is not a sleep."""
    from analyst_agent.eval import runner

    t = {"now": 0.0}
    det = runner.HostSleepDetector()
    orig = runner._wall_clock, runner._mono_clock
    runner._wall_clock = runner._mono_clock = lambda: t["now"]
    try:
        det.mark("start a")
        t["now"] += 900  # 15 minutes of work, both clocks advance together
        assert det.mark("start b") is None and det.events == []
    finally:
        runner._wall_clock, runner._mono_clock = orig
