from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import SimpleNamespace
from typing import Any

from analyst_agent.api.agent import AgentOutcome, EventedLLM, EventTranslator, coerce_outcome


class RunStatus(StrEnum):  # mirrors analyst_agent.core.loop.RunStatus
    COMPLETED = "completed"
    COMPLETED_WITH_ISSUES = "completed_with_issues"
    BUDGET_EXCEEDED = "budget_exceeded"
    MAX_TURNS = "max_turns"
    REFUSED = "refused"
    ERROR = "error"


class FakeCostTracker:
    def to_dict(self) -> dict[str, Any]:
        return {"input_tokens": 5, "output_tokens": 2, "usd": 0.1, "calls": 3}


@dataclass
class FakeState:
    artifacts: list[str] = field(default_factory=lambda: ["artifacts/a.png"])
    detail: str = ""


@dataclass
class FakeAgentResult:  # shape of analyst_agent.core.loop.AgentResult
    status: RunStatus
    report: str
    key_numbers: dict[str, Any]
    state: FakeState

    @property
    def cost(self) -> FakeCostTracker:
        return FakeCostTracker()


def _result(status: RunStatus, detail: str = "") -> FakeAgentResult:
    return FakeAgentResult(status, "# Report", {"n": 1}, FakeState(detail=detail))


def test_coerce_agent_result_completed():
    out = coerce_outcome(_result(RunStatus.COMPLETED_WITH_ISSUES, "critic unavailable"))
    assert out.status == "completed"
    assert out.agent_status == "completed_with_issues"
    assert out.report_markdown == "# Report"
    assert out.key_numbers == {"n": 1}
    assert out.artifacts == ["artifacts/a.png"]
    assert out.cost == {"input_tokens": 5, "output_tokens": 2, "usd": 0.1, "calls": 3}
    assert out.error is None


def test_coerce_agent_result_partial_and_failed():
    partial = coerce_outcome(_result(RunStatus.BUDGET_EXCEEDED, "token budget hit"))
    assert (partial.status, partial.error) == ("partial", "token budget hit")
    assert coerce_outcome(_result(RunStatus.MAX_TURNS)).status == "partial"
    assert coerce_outcome(_result(RunStatus.REFUSED)).status == "failed"
    assert coerce_outcome(_result(RunStatus.ERROR, "api down")).error == "api down"


def test_coerce_other_shapes():
    assert coerce_outcome("just text").report_markdown == "just text"
    assert coerce_outcome({"status": "error", "error": "x"}).status == "failed"
    same = AgentOutcome(report_markdown="r")
    assert coerce_outcome(same) is same


def test_event_translator_pairs_tool_calls_and_adds_progress():
    out: list[tuple[str, dict]] = []
    hook = EventTranslator(lambda t, d: out.append((t, d)), max_turns=10)
    hook("plan", {"plan": "p", "turn": 1})
    hook("tool_call", {"name": "run_sql", "input": {"query": "a"}, "turn": 1})
    hook("tool_call", {"name": "run_sql", "input": {"query": "b"}, "turn": 1})
    hook("tool_result", {"name": "run_sql", "is_error": False, "summary": "ok a"})
    hook("tool_result", {"name": "run_sql", "is_error": True, "summary": "bad b"})
    hook("stop", {"status": "max_turns", "detail": "limit"})

    assert out[0] == ("plan", {"plan": "p", "turn": 1, "max_turns": 10})
    calls = [d for t, d in out if t == "tool_call"]
    results = [d for t, d in out if t == "tool_result"]
    assert [c["id"] for c in calls] == [r["id"] for r in results] == ["call_1", "call_2"]
    assert [r["ok"] for r in results] == [True, False]
    assert "is_error" not in results[0]
    assert out[-1] == ("stop", {"status": "max_turns", "detail": "limit"})


def test_evented_llm_emits_one_llm_call_per_request():
    usage = SimpleNamespace(
        input_tokens=100, output_tokens=20, cache_read_input_tokens=50,
        cache_creation_input_tokens=0,
    )  # fmt: skip
    response = SimpleNamespace(usage=usage, model="claude-x", stop_reason="tool_use")

    class Inner:
        settings = "inner-settings"

        def __init__(self) -> None:
            self.kwargs: list[dict] = []

        def create(self, **kwargs: Any) -> Any:
            self.kwargs.append(kwargs)
            return response

    inner, events = Inner(), []
    llm = EventedLLM(inner, lambda t, d: events.append((t, d)), cost_fn=lambda m, u: 0.0042)
    assert llm.create(system="s", messages=[], role="critic") is response
    assert inner.kwargs == [{"system": "s", "messages": [], "role": "critic"}]
    assert llm.settings == "inner-settings"  # attribute passthrough

    ((kind, data),) = events
    assert kind == "llm_call"
    assert data["model"] == "claude-x" and data["purpose"] == "critic"
    assert (data["input_tokens"], data["output_tokens"], data["cache_read_tokens"]) == (100, 20, 50)
    assert data["usd"] == 0.0042 and data["latency_ms"] >= 0


def test_evented_llm_pricing_failure_is_swallowed():
    response = SimpleNamespace(usage=SimpleNamespace(), model="m", stop_reason=None)
    inner = SimpleNamespace(create=lambda **_: response)
    events: list = []

    def broken(*_):
        raise KeyError("unknown model")

    EventedLLM(inner, lambda t, d: events.append(d), cost_fn=broken).create(system="s")
    assert events[0]["usd"] == 0.0


def test_coerce_carries_prompt_versions():
    versions = {"planner": {"version": "1.0.0", "sha256": "abc"}}
    assert coerce_outcome({"status": "completed", "prompts": versions}).prompts == versions
    assert coerce_outcome(_result(RunStatus.COMPLETED)).prompts == {}  # no to_dict, no prompts
