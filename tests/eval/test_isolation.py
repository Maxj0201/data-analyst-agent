"""Process isolation: timed-out agent runs are really killed, not left running in a thread."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from analyst_agent.eval.agent import dry_run_agent
from analyst_agent.eval.runner import RunConfig, run_task, run_tasks
from analyst_agent.eval.schema import load_tasks
from tests.eval.agents import SleepingCoreAgent, crashing_agent, echo_42, raising_agent
from tests.eval.conftest import echo_agent, make_task


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _l1_task():
    return load_tasks(difficulties=["L1"], limit=1)[0]


@pytest.mark.asyncio
async def test_timed_out_core_agent_process_is_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_ARTIFACTS_DIR", str(tmp_path / "art"))
    log = tmp_path / "llm_calls.log"
    agent = SleepingCoreAgent(log, sleep_s=60)

    t0 = time.perf_counter()
    res = await run_task(_l1_task(), agent, None, timeout_s=8, isolation="process")
    elapsed = time.perf_counter() - t0

    assert res.status == "timeout" and not res.passed
    assert elapsed < 20
    lines = log.read_text().split()
    pid = int(lines[0])
    assert pid != os.getpid()  # the agent ran in a child process...
    assert len(log.read_text().splitlines()) == 2  # ...and reached the hanging LLM call
    assert not _alive(pid)  # ...which is gone, not sleeping on in the background

    time.sleep(0.5)
    assert len(log.read_text().splitlines()) == 2  # no further LLM calls after the kill


@pytest.mark.asyncio
async def test_inline_isolation_leaks_the_thread(tmp_path: Path) -> None:
    """Documents why process isolation exists: inline, the timed-out agent keeps going."""
    log = tmp_path / "llm_calls.log"
    agent = SleepingCoreAgent(log, sleep_s=1.5)
    task = _l1_task()
    res = await run_task(task, agent, None, timeout_s=1, isolation="inline")
    assert res.status == "timeout"
    pid = int(log.read_text().split()[0])
    assert pid == os.getpid()  # ran in a worker thread of this process


@pytest.mark.asyncio
async def test_child_errors_and_crashes_are_recorded() -> None:
    res = await run_task(make_task("L1"), raising_agent, None, timeout_s=30)
    assert res.status == "error" and "kaput" in (res.error or "")
    res = await run_task(make_task("L1"), crashing_agent, None, timeout_s=30)
    assert res.status == "error" and "exited with code 3" in (res.error or "")
    res = await run_task(make_task("L1"), echo_42, None, timeout_s=30)
    assert res.status == "ok" and res.passed and res.cost_usd == 0.01


@pytest.mark.asyncio
async def test_process_isolation_rejects_closures_up_front(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="isolation='inline'"):
        await run_tasks(
            [make_task("L1")],
            echo_agent(),
            config=RunConfig(isolation="process"),
            run_dir=tmp_path / "run",
            progress=False,
        )
    assert not (tmp_path / "run").exists()


@pytest.mark.asyncio
async def test_dry_run_core_agent_on_three_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The FakeLLM dry run drives the real loop, tools and sandbox to completion."""
    monkeypatch.setenv("AGENT_ARTIFACTS_DIR", str(tmp_path / "art"))
    tasks = load_tasks(limit=3)
    _, results = await run_tasks(
        tasks,
        dry_run_agent,
        config=RunConfig(score=False, timeout_s=120),
        run_dir=tmp_path / "run",
        progress=False,
    )
    assert [r.status for r in results] == ["ok"] * 3, [r.error for r in results]
    assert all(r.agent_status == "completed" for r in results)
    assert all(r.tool_calls >= 2 and r.cost_usd == 0 for r in results)


@pytest.mark.asyncio
async def test_dry_run_with_routing_forced_on_runs_in_child_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--routing`` must give a picklable agent, or process isolation would reject it."""
    import pickle

    from analyst_agent.eval.agent import CoreAgent, load_agent

    monkeypatch.setenv("AGENT_ARTIFACTS_DIR", str(tmp_path / "art"))
    agent = load_agent("dry-run", routing_enabled=True)
    assert agent == CoreAgent(dry_run=True, routing_enabled=True)
    assert pickle.loads(pickle.dumps(agent)) == agent
    _, results = await run_tasks(
        load_tasks(limit=2),
        agent,
        config=RunConfig(score=False, timeout_s=120),
        run_dir=tmp_path / "run",
        progress=False,
    )
    assert [r.status for r in results] == ["ok"] * 2, [r.error for r in results]
    assert all(r.routing and r.routing["route"]["source"] == "router" for r in results)


@pytest.mark.asyncio
@pytest.mark.parametrize("auto_approve", [True, False])
async def test_eval_auto_approves_gated_steps_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, auto_approve: bool
) -> None:
    """With every flag on, eval must not stall or deny on approval gates (no human attached)."""
    from analyst_agent.eval.agent import load_agent

    monkeypatch.setenv("AGENT_ARTIFACTS_DIR", str(tmp_path / "art"))
    for flag in ("ROUTING", "SEMANTIC_LAYER", "APPROVAL"):
        monkeypatch.setenv(f"AGENT_{flag}_ENABLED", "true")
    monkeypatch.setenv("AGENT_APPROVAL_ROW_THRESHOLD", "0")  # gate every SELECT
    agent = load_agent("dry-run", auto_approve=auto_approve)
    _, results = await run_tasks(
        load_tasks(limit=1),
        agent,
        config=RunConfig(score=False, timeout_s=120, auto_approve=auto_approve),
        run_dir=tmp_path / "run",
        progress=False,
    )
    (r,) = results
    assert r.status == "ok" and r.agent_status == "completed", r.error
    types = [e["type"] for e in r.trace]
    assert types[:2] == ["route", "semantic_retrieval"]
    approvals = [e for e in r.trace if e["type"] == "approval"]
    assert approvals and all(e["tool"] == "run_sql" for e in approvals)
    expected = (True, "auto") if auto_approve else (False, "unavailable")
    assert {(e["approved"], e["decided_by"]) for e in approvals} == {expected}


@pytest.mark.asyncio
@pytest.mark.parametrize("isolation", ["process", "inline"])
async def test_budget_counts_the_spend_of_timed_out_tasks(
    isolation: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review-2 M2: a task killed on timeout returned no output, so its LLM spend was recorded
    as $0 and ``--budget-usd`` kept starting tasks that each burned a full timeout."""
    monkeypatch.setenv("AGENT_ARTIFACTS_DIR", str(tmp_path / "art"))
    log = tmp_path / "llm_calls.log"
    agent = SleepingCoreAgent(log, sleep_s=60 if isolation == "process" else 3)
    tasks = [_l1_task().model_copy(update={"id": f"t{i}"}) for i in range(2)]
    _, results = await run_tasks(
        tasks,
        agent,
        config=RunConfig(
            concurrency=1, isolation=isolation, timeout_s=6, budget_usd=1e-6, score=False
        ),
        run_dir=tmp_path / "run",
        progress=False,
    )
    assert results[0].status == "timeout"
    assert results[0].cost_usd > 0  # the first LLM call was paid for before the hang
    assert results[1].status == "over_budget"
