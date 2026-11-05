"""Message Batches for the judge (``eval run --batch``), against a fake batches client."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import pytest

from analyst_agent.core.config import Settings
from analyst_agent.core.llm import (
    BatchRequestError,
    BatchUnsupported,
    CostTracker,
    LLMClient,
    Usage,
    estimate_cost_usd,
)
from analyst_agent.eval.agent import AgentInput, AgentOutput
from analyst_agent.eval.judge import BatchJudge, JudgeError, LLMJudge
from analyst_agent.eval.runner import PENDING_JUDGE, RunConfig, load_results, run_tasks
from tests.eval.conftest import make_task, report_quote

JUDGE_MODEL = "claude-sonnet-5-5"


def _verdict(params: dict[str, Any]) -> str:
    """What a judge would answer for this request (all rubric points / behaviour pass)."""
    schema = params["output_config"]["format"]["schema"]
    if "points" in schema["properties"]:
        user = params["messages"][0]["content"]
        n = user.split("<rubric>")[1].split("</rubric>")[0].strip().count("\n") + 1
        ev = [report_quote(user)] if report_quote(user) else []
        points = [
            {"index": i, "evidence": ev, "unmatched_numbers": [], "score": 1, "justification": "b"}
            for i in range(n)
        ]
        return json.dumps({"points": points})
    return json.dumps({"passed": True, "justification": "b"})


def _message(text: str, model: str = JUDGE_MODEL) -> NS:
    return NS(
        content=[{"type": "text", "text": text}],
        stop_reason="end_turn",
        usage=NS(input_tokens=1000, output_tokens=100),
        model=model,
    )


class FakeBatches:
    """``client.messages.batches``: ends after ``polls`` retrieves; results in reverse order."""

    def __init__(
        self,
        polls: int = 2,
        errored: set[int] | None = None,
        fail_create: Exception | None = None,
        never_ends: bool = False,
    ) -> None:
        self.polls, self.errored, self.fail_create = polls, errored or set(), fail_create
        self.never_ends = never_ends
        self.created: list[list[dict[str, Any]]] = []
        self.retrieves = 0
        self.cancelled: list[str] = []

    def create(self, *, requests: list[dict[str, Any]]) -> NS:
        if self.fail_create:
            raise self.fail_create
        self.created.append(list(requests))
        return NS(id=f"batch_{len(self.created)}", processing_status="in_progress")

    def retrieve(self, batch_id: str) -> NS:
        self.retrieves += 1
        ended = not self.never_ends and self.retrieves >= self.polls
        return NS(id=batch_id, processing_status="ended" if ended else "in_progress")

    def cancel(self, batch_id: str) -> NS:
        self.cancelled.append(batch_id)
        return NS(id=batch_id, processing_status="canceling")

    def results(self, batch_id: str) -> Any:
        requests = self.created[int(batch_id.split("_")[1]) - 1]
        for i, req in reversed(list(enumerate(requests))):
            if i in self.errored:
                err = NS(type="error", error=NS(type="overloaded_error", message="overloaded"))
                yield NS(custom_id=req["custom_id"], result=NS(type="errored", error=err))
            else:
                msg = _message(_verdict(req["params"]))
                yield NS(custom_id=req["custom_id"], result=NS(type="succeeded", message=msg))


class FakeMessages:
    def __init__(self, batches: FakeBatches | None) -> None:
        if batches is not None:
            self.batches = batches
        self.direct: list[dict[str, Any]] = []

    def create(self, **params: Any) -> NS:
        self.direct.append(params)
        return _message(_verdict(params))


class FakeClient:
    def __init__(self, batches: FakeBatches | None) -> None:
        self.messages = FakeMessages(batches)
        self.beta = NS(messages=self.messages)  # refusal-fallback path, same fake


def _client(batches: FakeBatches | None) -> tuple[LLMClient, FakeClient]:
    fake = FakeClient(batches)
    settings = Settings(_env_file=None, effort="low", refusal_fallback=False)  # type: ignore[call-arg]
    return LLMClient(settings, client=fake, sleep=lambda _s: None), fake


def _req(text: str) -> dict[str, Any]:
    return {
        "system": "sys",
        "messages": [{"role": "user", "content": text}],
        "output_format": {
            "type": "json_schema",
            "schema": {"type": "object", "properties": {"passed": {"type": "boolean"}}},
        },
        "model": JUDGE_MODEL,
    }


# ---------------------------------------------------------------- core.llm.LLMClient.create_batch


def test_create_batch_returns_results_in_request_order_at_half_price() -> None:
    batches = FakeBatches(polls=3)
    llm, _ = _client(batches)
    cost = CostTracker()
    out = llm.create_batch([_req("a"), _req("b"), _req("c")], role="judge", cost=cost)
    assert len(out) == 3 and all(not isinstance(r, Exception) for r in out)
    assert [r.text for r in out] == [json.dumps({"passed": True, "justification": "b"})] * 3
    sent = batches.created[0]
    assert [r["custom_id"] for r in sent] == ["req-0", "req-1", "req-2"]
    params = sent[0]["params"]
    assert params["model"] == JUDGE_MODEL and params["output_config"]["effort"] == "low"
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert batches.retrieves == 3  # polled until ended
    full = estimate_cost_usd(JUDGE_MODEL, Usage(1000, 100))
    assert cost.usd == pytest.approx(3 * full * 0.5)
    assert cost.by_role == {"judge": pytest.approx(3 * full * 0.5)} and cost.calls == 3


def test_create_batch_marks_errored_requests() -> None:
    llm, _ = _client(FakeBatches(errored={1}))
    out = llm.create_batch([_req("a"), _req("b")])
    assert not isinstance(out[0], Exception)
    assert isinstance(out[1], BatchRequestError) and "overloaded" in str(out[1])


def test_create_batch_unsupported_and_timeout() -> None:
    llm, _ = _client(None)
    assert not llm.supports_batches
    with pytest.raises(BatchUnsupported):
        llm.create_batch([_req("a")])
    assert llm.create_batch([]) == []

    batches = FakeBatches(never_ends=True)
    llm, _ = _client(batches)
    assert llm.supports_batches
    with pytest.raises(TimeoutError):
        llm.create_batch([_req("a")], poll_interval_s=10, max_wait_s=30)
    assert batches.cancelled == ["batch_1"]


# ---------------------------------------------------------------- BatchJudge in the runner


def _tasks() -> list[Any]:
    return [
        make_task("L1").model_copy(update={"id": "l1"}),
        make_task("L3").model_copy(update={"id": "l3a"}),
        make_task("L3").model_copy(update={"id": "l3b"}),
        make_task("ADV").model_copy(update={"id": "adv"}),
        make_task("L2").model_copy(update={"id": "l2"}),
    ]


async def _agent(inp: AgentInput) -> AgentOutput:
    return AgentOutput(answer="The answer is 42.", cost_usd=0.01)


async def _run(tmp_path: Path, judge: Any, concurrency: int = 2) -> tuple[Path, list[Any]]:
    return await run_tasks(
        _tasks(),
        _agent,
        judge=judge,
        config=RunConfig(concurrency=concurrency, isolation="inline", batch_judge=True),
        run_dir=tmp_path / "run",
        progress=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [1, 4])
async def test_batch_judge_sends_one_batch_per_run(tmp_path: Path, concurrency: int) -> None:
    batches = FakeBatches()
    llm, fake = _client(batches)
    judge = BatchJudge(LLMJudge(llm=llm, model=JUDGE_MODEL, cost=CostTracker()))
    run_dir, results = await _run(tmp_path, judge, concurrency)

    assert len(batches.created) == 1 and len(batches.created[0]) == 3  # l3a, l3b, adv
    assert fake.messages.direct == []
    assert {r.task_id: r.passed for r in results} == {
        "l1": True,
        "l3a": True,
        "l3b": True,
        "adv": True,
        "l2": False,
    }
    rows = load_results(run_dir)
    assert len(rows) == 5  # one row per task once the duplicates are folded
    assert all(r["score_details"]["method"] != PENDING_JUDGE for r in rows)
    raw = (run_dir / "results.jsonl").read_text().splitlines()
    pending = [json.loads(ln) for ln in raw if PENDING_JUDGE in ln]
    assert sorted(p["task_id"] for p in pending) == ["adv", "l3a", "l3b"]
    assert len(raw) == 5 + 3  # a pending row, then the verdict, for every judged task
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["judge_batch"] == {
        "batches": 1,
        "batched_requests": 3,
        "direct_requests": 0,
        "fallback": None,
    }
    assert meta["config"]["batch_judge"] is True
    judge_usd = 3 * estimate_cost_usd(JUDGE_MODEL, Usage(1000, 100)) * 0.5
    assert meta["spend_usd"] == pytest.approx(5 * 0.01 + judge_usd)


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [1, 4])
async def test_judge_samples_still_send_one_batch(tmp_path: Path, concurrency: int) -> None:
    # Each L3 task queues 3 calls: the flush must wait for every task, not count queued calls.
    batches = FakeBatches()
    llm, fake = _client(batches)
    judge = BatchJudge(LLMJudge(llm=llm, model=JUDGE_MODEL, cost=CostTracker()))
    _, results = await run_tasks(
        _tasks(),
        _agent,
        judge=judge,
        config=RunConfig(
            concurrency=concurrency, isolation="inline", batch_judge=True, judge_samples=3
        ),
        run_dir=tmp_path / "run",
        progress=False,
    )
    assert len(batches.created) == 1 and len(batches.created[0]) == 3 + 3 + 1  # l3a, l3b, adv
    assert fake.messages.direct == []
    by_id = {r.task_id: r for r in results}
    assert by_id["l3a"].passed and by_id["l3a"].score_details["judge_samples"]["n"] == 3
    assert "judge_samples" not in by_id["adv"].score_details


class NoBatchLLM:
    """A core.llm.LLM without create_batch (e.g. a test fake or an old SDK client)."""

    def __init__(self) -> None:
        self.calls = 0

    def create(self, **kw: Any) -> Any:
        self.calls += 1
        params = {
            "messages": kw["messages"],
            "output_config": {"format": kw["output_format"]},
        }
        return NS(text=_verdict(params), stop_reason="end_turn")


@pytest.mark.asyncio
async def test_batch_judge_falls_back_silently_without_sdk_support(tmp_path: Path) -> None:
    llm = NoBatchLLM()
    judge = BatchJudge(LLMJudge(llm=llm))
    run_dir, results = await _run(tmp_path, judge)
    assert llm.calls == 3 and sum(r.passed for r in results) == 4
    stats = json.loads((run_dir / "meta.json").read_text())["judge_batch"]
    assert stats["batches"] == 0 and stats["direct_requests"] == 3
    assert "does not support" in stats["fallback"]


@pytest.mark.asyncio
async def test_batch_judge_falls_back_when_the_batch_fails(tmp_path: Path) -> None:
    llm, fake = _client(FakeBatches(fail_create=RuntimeError("batches disabled for this org")))
    judge = BatchJudge(LLMJudge(llm=llm, model=JUDGE_MODEL))
    run_dir, results = await _run(tmp_path, judge)
    assert len(fake.messages.direct) == 3 and sum(r.passed for r in results) == 4
    stats = json.loads((run_dir / "meta.json").read_text())["judge_batch"]
    assert "batches disabled" in stats["fallback"] and stats["direct_requests"] == 3


@pytest.mark.asyncio
async def test_errored_batch_items_are_retried_directly(tmp_path: Path) -> None:
    llm, fake = _client(FakeBatches(errored={0}))
    judge = BatchJudge(LLMJudge(llm=llm, model=JUDGE_MODEL))
    run_dir, results = await _run(tmp_path, judge)
    assert len(fake.messages.direct) == 1 and sum(r.passed for r in results) == 4
    stats = json.loads((run_dir / "meta.json").read_text())["judge_batch"]
    assert stats == {"batches": 1, "batched_requests": 2, "direct_requests": 1, "fallback": None}


@pytest.mark.asyncio
async def test_batch_judge_unavailable_marks_tasks_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    judge = BatchJudge(LLMJudge())  # no llm and no key: JudgeUnavailable at flush time
    _, results = await _run(tmp_path, judge)
    by_id = {r.task_id: r for r in results}
    assert by_id["l3a"].score_details["skipped"] is True
    assert by_id["adv"].score_details["judge"]["skipped"] is True


@pytest.mark.asyncio
async def test_flush_never_leaves_a_scorer_hanging() -> None:
    class Broken(LLMJudge):
        @property
        def llm(self) -> Any:
            raise RuntimeError("unexpected")

    judge = BatchJudge(Broken())
    waiter = asyncio.ensure_future(judge.complete("s", "u", {"properties": {}}))
    await asyncio.sleep(0)
    assert judge.pending == 1
    await judge.flush()
    with pytest.raises(JudgeError, match="judge batch failed"):
        await waiter
