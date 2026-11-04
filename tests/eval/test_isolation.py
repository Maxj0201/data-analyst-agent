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
