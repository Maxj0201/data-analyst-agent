"""``--budget-usd`` x ``--batch`` x ``--resume`` (review-2 M2 merged with #22's runner split).

The cap counts agent spend (including the metered spend of tasks that time out), direct judge
calls and batched judge calls; a resumed run starts from everything the earlier run recorded,
also when that run was killed before it finished.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from analyst_agent.core.llm import CostTracker, Usage, estimate_cost_usd
from analyst_agent.eval.agent import AgentInput, AgentOutput, report_spend
from analyst_agent.eval.judge import BatchJudge, LLMJudge
from analyst_agent.eval.runner import RunConfig, load_results, run_tasks
from tests.eval.conftest import make_task
from tests.eval.test_batch_judge import JUDGE_MODEL, FakeBatches, _client

CALL_USD = estimate_cost_usd(JUDGE_MODEL, Usage(1000, 100))  # one judge call at list price


class Agent:
    """Costs 0.4 per task. ``slow`` reports 0.3 and then outlives its timeout; ``hang``
    (with ``hang=True``) signals ``started`` and blocks until the run is killed."""

    def __init__(self, hang: bool = False) -> None:
        self.hang = hang
        self.calls: list[str] = []
        self.started = asyncio.Event()

    async def __call__(self, inp: AgentInput) -> AgentOutput:
        self.calls.append(inp.task_id)
        if inp.task_id == "slow":
            report_spend(0.3)
            await asyncio.sleep(60)
        if inp.task_id == "hang" and self.hang:
            self.started.set()
            await asyncio.sleep(3600)
        return AgentOutput(answer="The answer is 42.", cost_usd=0.4)


def _judge(batch: bool, batches: FakeBatches | None = None) -> Any:
    if batch:
        llm, _ = _client(batches or FakeBatches())
        return BatchJudge(LLMJudge(llm=llm, model=JUDGE_MODEL, cost=CostTracker()))
    llm, _ = _client(None)  # no messages.batches: every verdict is a direct call
    return LLMJudge(llm=llm, model=JUDGE_MODEL, cost=CostTracker())


def _tasks(*spec: tuple[str, str]) -> list[Any]:
    return [make_task(level).model_copy(update={"id": tid}) for tid, level in spec]


def _meta(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / "meta.json").read_text())


async def _run(
    tmp_path: Path, tasks: list[Any], agent: Any, judge: Any, budget: float | None, **kw: Any
) -> tuple[Path, list[Any]]:
    cfg = RunConfig(
        concurrency=1,
        isolation="inline",
        budget_usd=budget,
        timeout_s=kw.pop("timeout_s", 30.0),
        batch_judge=isinstance(judge, BatchJudge),
    )
    return await run_tasks(
        tasks, agent, judge=judge, config=cfg, run_dir=tmp_path / "run", progress=False, **kw
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True], ids=["direct-judge", "batched-judge"])
async def test_resume_cap_counts_judge_spend_of_the_earlier_run(
    tmp_path: Path, batch: bool
) -> None:
    tasks = _tasks(("l3a", "L3"), ("l3b", "L3"), ("l1a", "L1"), ("l1b", "L1"))
    run_dir, results = await _run(tmp_path, tasks, Agent(), _judge(batch), budget=1.0)
    # 0.8 (+ judge) < 1.0 before l1a, 1.2 after it: l1b is never started.
    assert [r.status for r in results] == ["ok", "ok", "ok", "over_budget"]
    judge_usd = 2 * CALL_USD * (0.5 if batch else 1.0)
    assert _meta(run_dir)["spend_usd"] == pytest.approx(1.2 + judge_usd)

    # Without the judge's spend the resumed run would see 1.2 < cap and start l1b.
    agent = Agent()
    _, results = await _run(
        tmp_path, tasks, agent, _judge(batch), budget=1.2 + judge_usd / 2, resume=True
    )
    assert agent.calls == [] and results[-1].status == "over_budget"
    assert _meta(run_dir)["resumes"][0]["prior_spend_usd"] == pytest.approx(1.2 + judge_usd)


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True], ids=["direct-judge", "batched-judge"])
async def test_killed_run_resumes_with_the_spend_it_recorded(tmp_path: Path, batch: bool) -> None:
    tasks = _tasks(("l3", "L3"), ("slow", "L1"), ("hang", "L1"), ("l1", "L1"))
    agent, batches = Agent(hang=True), FakeBatches()
    judge = _judge(batch, batches)
    run = asyncio.ensure_future(_run(tmp_path, tasks, agent, judge, None, timeout_s=1.0))
    await asyncio.wait_for(agent.started.wait(), 20)
    run.cancel()  # what Ctrl-C does to `eval run` (asyncio.run cancels the main task)
    with pytest.raises(asyncio.CancelledError):
        await run
    await asyncio.sleep(0.2)
    assert batches.created == []  # no batch is sent while the run is being cancelled

    run_dir = tmp_path / "run"
    rows = {r["task_id"]: r for r in load_results(run_dir)}
    assert rows["slow"]["status"] == "timeout"
    assert rows["slow"]["cost_usd"] == pytest.approx(0.3)  # metered, though it never returned
    # l3 was paid for (0.4) and judged directly; with --batch its verdict was still queued.
    judge_usd = 0.0 if batch else CALL_USD
    recorded = 0.4 + 0.3 + judge_usd
    meta = _meta(run_dir)
    assert "finished_at" not in meta and meta["spend_usd"] == pytest.approx(recorded)

    again = Agent()
    _, results = await _run(tmp_path, tasks, again, _judge(batch), None, resume=True)
    assert again.calls == ["hang", "l1"]  # l3 (done or re-scored) and slow (timeout) are not
    assert [r.status for r in results] == ["ok", "timeout", "ok", "ok"]
    meta = _meta(run_dir)
    assert meta["resumes"][0]["prior_spend_usd"] == pytest.approx(recorded)
    # The resumed run adds two agent runs, plus the queued verdict (batched, half price).
    extra_judge = CALL_USD * 0.5 if batch else 0.0
    assert meta["spend_usd"] == pytest.approx(recorded + 0.8 + extra_judge)
