"""Eval results pushed to Langfuse as trace scores, and span trees saved per task."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import analyst_agent.eval.agent as eval_agent
from analyst_agent.core.langfuse_export import eval_scores, judge_score
from analyst_agent.core.tracing import find_trace, load_trace
from analyst_agent.eval.agent import AgentInput, AgentOutput, dry_run_agent
from analyst_agent.eval.runner import RunConfig, load_results, run_tasks
from analyst_agent.eval.schema import load_tasks
from tests.eval.conftest import MockJudge, make_task
from tests.fake_langfuse import FakeLangfuse

# --------------------------------------------------------------------------- score mapping


def test_scored_task_gets_accuracy_passed_cost_and_judge() -> None:
    details = {
        "method": "rubric_judge",
        "points": [{"score": 1}, {"score": 0}, {"score": 1}, {"score": 1}],
    }
    scores = eval_scores(status="ok", passed=True, score=0.75, score_details=details, cost_usd=0.2)
    by_name = {s["name"]: s for s in scores}
    assert by_name["accuracy"] == {"name": "accuracy", "value": 0.75, "data_type": "NUMERIC"}
    assert by_name["passed"]["value"] == 1.0 and by_name["passed"]["data_type"] == "BOOLEAN"
    assert by_name["judge_score"]["value"] == 0.75
    assert by_name["cost_usd"]["value"] == 0.2
    assert by_name["eval_status"] == {
        "name": "eval_status",
        "value": "ok",
        "data_type": "CATEGORICAL",
    }


@pytest.mark.parametrize(
    ("details", "expected"),
    [
        ({"method": "exact", "match": True}, None),  # no judge involved
        ({"method": "rubric_judge", "skipped": True}, None),
        ({"method": "rubric_judge", "error": "boom"}, None),
        ({"method": "adversarial", "judge": {"passed": False}}, 0.0),
        ({"method": "adversarial", "judge": {"passed": True}}, 1.0),
        ({"method": "adversarial", "judge": {"skipped": True}}, None),
    ],
)
def test_judge_score(details: dict[str, Any], expected: float | None) -> None:
    assert judge_score(details) == expected


def test_judge_less_scores_push_no_accuracy_or_judge_score() -> None:
    """#17 makes judge-less L3 tasks skipped and ADV rules-only; neither may reach Langfuse as
    a 0 accuracy or a 0 judge score."""
    l3 = {"method": "rubric_judge", "skipped": True, "error": "no judge configured"}
    names = {
        s["name"]
        for s in eval_scores(status="ok", passed=False, score=0.0, score_details=l3, cost_usd=0.0)
    }
    assert names == {"eval_status", "cost_usd"}
    adv = {"method": "adversarial", "judge": {"skipped": True, "error": "no judge"}}
    scores = eval_scores(status="ok", passed=True, score=1.0, score_details=adv, cost_usd=0.0)
    assert "judge_score" not in {s["name"] for s in scores}
    assert {s["name"]: s["value"] for s in scores}["passed"] == 1.0


def test_failed_runs_are_filterable_as_not_passed_but_have_no_accuracy() -> None:
    timeout = eval_scores(
        status="timeout", passed=False, score=0.0, score_details={"method": "not_run"}, cost_usd=0
    )
    assert {s["name"]: s["value"] for s in timeout} == {
        "eval_status": "timeout",
        "cost_usd": 0.0,
        "passed": 0.0,
    }
    skipped = eval_scores(
        status="ok",
        passed=False,
        score=0.0,
        score_details={"method": "rubric_judge", "skipped": True},
        cost_usd=0.1,
    )
    assert {s["name"] for s in skipped} == {"eval_status", "cost_usd"}  # not graded: no pass/fail


# --------------------------------------------------------------------------- runner


@pytest.fixture
def fake_lf(monkeypatch: pytest.MonkeyPatch) -> FakeLangfuse:
    client = FakeLangfuse()
    # The core agents build their own client for the span mirror (in the child process in
    # production); inline isolation lets the test hand them the fake.
    monkeypatch.setattr(eval_agent, "make_langfuse_client", lambda: client)
    return client


@pytest.mark.asyncio
async def test_dry_run_tasks_are_traced_and_scored_in_langfuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_lf: FakeLangfuse
) -> None:
    monkeypatch.setenv("AGENT_ARTIFACTS_DIR", str(tmp_path / "art"))
    tasks = load_tasks(difficulties=["L1"], limit=2)
    run_dir, results = await run_tasks(
        tasks,
        dry_run_agent,
        config=RunConfig(
            isolation="inline", timeout_s=120, langfuse_enabled=True, save_traces=True
        ),
        run_dir=tmp_path / "run",
        progress=False,
        langfuse=fake_lf,
    )
    assert [r.status for r in results] == ["ok", "ok"], [r.error for r in results]

    roots = {r.trace_id: r for r in fake_lf.roots}
    for res, task in zip(results, tasks, strict=True):
        # One Langfuse trace per task, recorded under the id the runner chose up front ...
        root = roots[res.trace_id]
        assert root.name == "analyst-eval" and root.as_type == "agent"
        assert root.metadata["task_id"] == task.id and root.metadata["run_id"] == res.run_id
        assert root.metadata["eval_run"] == run_dir.name
        assert [c.as_type for c in root.children if c.name.startswith("turn")]
        # ... and the scores land on that same trace.
        scores = fake_lf.scores_for(res.trace_id)
        assert scores["accuracy"] == res.score
        assert scores["passed"] == (1.0 if res.passed else 0.0)
        assert scores["eval_status"] == "ok" and scores["cost_usd"] == res.cost_usd == 0
        assert "judge_score" not in scores  # L1: exact-match scoring, no judge
        meta = next(s for s in fake_lf.scores if s["trace_id"] == res.trace_id)["metadata"]
        assert meta["task_id"] == task.id and meta["difficulty"] == "L1"

        # Span tree saved next to the results, findable by run id or task id.
        path = run_dir / "traces" / f"{res.run_id}.json"
        assert res.trace_path == str(path)
        assert find_trace(res.run_id, [tmp_path]) == path
        assert find_trace(task.id, [run_dir / "traces"]) == path
        assert load_trace(path).metadata["task_id"] == task.id

    rows = load_results(run_dir)
    assert all(r["trace_id"] and r["run_id"] and r["trace_path"] for r in rows)
    assert all("spans" not in r for r in rows)  # trees live in traces/, not in results.jsonl
    assert fake_lf.flushed >= 1


@pytest.mark.asyncio
async def test_judge_scores_and_timeouts_reach_langfuse(tmp_path: Path) -> None:
    lf = FakeLangfuse()

    async def slow_or_fast(inp: AgentInput) -> AgentOutput:
        if inp.task_id == "t_adv":
            await asyncio.sleep(5)
        assert inp.trace_id and len(inp.trace_id) == 32
        return AgentOutput(answer="- point a\n- point b", cost_usd=0.03)

    tasks = [make_task("L3"), make_task("ADV")]
    _, results = await run_tasks(
        tasks,
        slow_or_fast,
        judge=MockJudge(rubric_scores=[1, 0]),
        config=RunConfig(isolation="inline", timeout_s=0.5, langfuse_enabled=True),
        run_dir=tmp_path / "run",
        progress=False,
        langfuse=lf,
    )
    l3, adv = results
    assert lf.scores_for(l3.trace_id) == {
        "eval_status": "ok",
        "cost_usd": 0.03,
        "accuracy": 0.5,
        "passed": 1.0,  # pass_threshold 0.5
        "judge_score": 0.5,
    }
    assert adv.status == "timeout"
    assert lf.scores_for(adv.trace_id) == {"eval_status": "timeout", "cost_usd": 0.0, "passed": 0.0}
    assert l3.trace_path is None  # AGENT_TRACE_STORE_ENABLED off and the agent sent no spans


@pytest.mark.asyncio
async def test_langfuse_off_means_no_trace_ids_and_no_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import analyst_agent.eval.runner as runner

    def boom() -> None:
        raise AssertionError("must not create a Langfuse client when disabled")

    monkeypatch.setattr(runner, "make_langfuse_client", boom)

    async def agent(inp: AgentInput) -> AgentOutput:
        assert inp.trace_id is None
        return AgentOutput(answer="42")

    _, [res] = await run_tasks(
        [make_task("L1")],
        agent,
        config=RunConfig(isolation="inline", langfuse_enabled=False),
        run_dir=tmp_path / "run",
        progress=False,
    )
    assert res.trace_id is None and res.status == "ok"


@pytest.mark.asyncio
async def test_spans_survive_process_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child process returns the span tree; the parent saves it."""
    monkeypatch.setenv("AGENT_ARTIFACTS_DIR", str(tmp_path / "art"))
    run_dir, [res] = await run_tasks(
        load_tasks(limit=1),
        dry_run_agent,
        config=RunConfig(score=False, timeout_s=120, save_traces=True, langfuse_enabled=False),
        run_dir=tmp_path / "run",
        progress=False,
    )
    assert res.status == "ok", res.error
    doc = json.loads(Path(res.trace_path or "").read_text())
    assert doc["run_id"] == res.run_id and doc["task_id"] == res.task_id
    kinds = {s["kind"] for s in doc["spans"]["children"]}
    assert "turn" in kinds
