"""Span tree (core/tracing.py), its Langfuse mirror (core/langfuse_export.py) and rendering."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from analyst_agent.core.approval import AutoApprover
from analyst_agent.core.config import Settings
from analyst_agent.core.langfuse_export import LangfuseSink
from analyst_agent.core.llm import estimate_cost_usd
from analyst_agent.core.loop import AgentLoop, RunStatus
from analyst_agent.core.prompts import prompt_versions
from analyst_agent.core.tracing import (
    EventedLLM,
    Span,
    TraceRecorder,
    find_trace,
    load_trace,
    render_tree,
    save_trace,
)
from analyst_agent.tools import build_default_registry
from tests.conftest import FakeLLM, critic_json, resp, text, tool_use
from tests.fake_langfuse import FakeLangfuse, FakeObservation

ROUTER, M1, M2 = "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def tick(self, s: float) -> None:
        self.t += s


def llm_event(purpose: str, model: str = M2, usd: float = 0.01, latency_ms: float = 500) -> dict:
    return {
        "model": model,
        "purpose": purpose,
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_tokens": 10,
        "usd": usd,
        "latency_ms": latency_ms,
        "stop_reason": "tool_use",
    }


def shape(span: Span) -> Any:
    """``(kind, name, [children])`` with leaves collapsed to ``(kind, name)``."""
    if not span.children:
        return (span.kind, span.name)
    return (span.kind, span.name, [shape(c) for c in span.children])


def lf_shape(obs: FakeObservation) -> Any:
    if not obs.children:
        return (obs.as_type, obs.name)
    return (obs.as_type, obs.name, [lf_shape(c) for c in obs.children])


# --------------------------------------------------------------------------- recorder unit


def scripted_run(rec: TraceRecorder, clock: Clock) -> None:
    """The API's event sequence for a routed run with an approval gate and one critic revise."""
    rec.on_event(
        "run_start",
        {"run_id": "run_x", "model": M2, "prompts": prompt_versions(), "tables": ["sales"]},
    )
    clock.tick(0.3)
    rec.on_event("llm_call", llm_event("router", ROUTER, usd=0.0001, latency_ms=300))
    rec.on_event("route", {"tier": "L1", "model": M1, "reason": "simple", "source": "router"})
    rec.on_event("turn", {"turn": 1, "model": M1, "tier": "L1"})
    clock.tick(1.0)
    rec.on_event("llm_call", llm_event("agent", M1, usd=0.02, latency_ms=1000))
    rec.on_event("plan", {"plan": "1. sum\n2. report", "turn": 1})
    rec.on_event("tool_call", {"id": "c1", "name": "run_python", "input": {"code": "1"}})
    rec.on_event(
        "approval_required", {"id": "a1", "tool": "run_python", "reasons": ["python"], "turn": 1}
    )
    clock.tick(2.0)
    rec.on_event(
        "approval_decision",
        {"action_id": "a1", "tool": "run_python", "approved": True, "decided_by": "human"},
    )
    clock.tick(0.5)
    rec.on_event("tool_result", {"id": "c1", "name": "run_python", "ok": True, "summary": "1"})
    rec.on_event("turn", {"turn": 2, "model": M1, "tier": "L1"})
    clock.tick(1.0)
    rec.on_event("llm_call", llm_event("agent", M1, usd=0.03, latency_ms=1000))
    rec.on_event("tool_call", {"id": "c2", "name": "final_report", "input": {}})
    rec.on_event("tool_result", {"id": "c2", "name": "final_report", "ok": True, "summary": "ok"})
    clock.tick(0.8)
    rec.on_event("llm_call", llm_event("critic", M2, usd=0.04, latency_ms=800))
    rec.on_event("critic", {"verdict": "revise", "issues": ["wrong"], "round": 1})
    rec.on_event(
        "escalation",
        {"from_tier": "L1", "to_tier": "L2", "trigger": "critic_revise", "to_model": M2},
    )
    rec.on_event("turn", {"turn": 3, "model": M2, "tier": "L2"})
    clock.tick(1.0)
    rec.on_event("llm_call", llm_event("agent", M2, usd=0.05, latency_ms=1000))
    rec.on_event("stop", {"status": "max_turns", "detail": "limit"})
    clock.tick(0.4)
    rec.on_event("llm_call", llm_event("report_writer", M2, usd=0.06, latency_ms=400))
    rec.finish(status="partial", output="# report")


def test_recorder_builds_run_turn_tree_with_nested_llm_calls() -> None:
    clock = Clock()
    rec = TraceRecorder(metadata={"job_id": "job_1"}, question="q?", clock=clock)
    scripted_run(rec, clock)
    root = rec.root

    assert shape(root) == (
        "run",
        "run",
        [
            ("route", "route", [("llm_call", "llm:router")]),
            (
                "turn",
                "turn 1",
                [
                    ("llm_call", "llm:agent"),
                    ("plan", "plan"),
                    ("tool_call", "tool:run_python", [("approval", "approval:run_python")]),
                ],
            ),
            (
                "turn",
                "turn 2",
                [
                    ("llm_call", "llm:agent"),
                    ("tool_call", "tool:final_report"),
                    ("critic", "critic", [("llm_call", "llm:critic")]),
                    ("escalation", "escalation:L1->L2"),
                ],
            ),
            ("turn", "turn 3", [("llm_call", "llm:agent")]),
            ("llm_call", "llm:report_writer"),
        ],
    )
    route, t1, t2, t3, writer = root.children

    # Cost: own cost on llm spans, rolled up on containers and the run.
    assert root.total_cost() == pytest.approx(0.2001)
    assert root.metadata["cost_usd"] == pytest.approx(0.2001)
    assert t2.total_cost() == pytest.approx(0.07)  # agent + critic
    assert t2.metadata["cost_usd"] == pytest.approx(0.07)

    # Model, prompt version and routed tier metadata.
    md = root.metadata
    assert md["run_id"] == "run_x" and md["job_id"] == "job_1"
    assert md["routed_tier"] == "L1" and md["final_tier"] == "L2"
    assert md["models"] == sorted({ROUTER, M1, M2}) and md["llm_calls"] == 6
    assert md["prompt_versions"]["planner"] == prompt_versions()["planner"]["version"]
    assert md["status"] == "partial" and md["agent_status"] == "max_turns"
    agent1 = t1.children[0]
    assert agent1.metadata["model"] == M1 and agent1.metadata["tier"] == "L1"
    assert agent1.metadata["prompt"] == "planner" and agent1.metadata["prompt_version"]
    assert t3.children[0].metadata["tier"] == "L2"  # after the escalation
    assert route.children[0].metadata["prompt"] == "router"
    assert t1.metadata["tier"] == "L1" and t3.metadata["model"] == M2

    # Timings: the llm span is back-dated by its latency; the approval wait is its own span.
    assert agent1.duration == pytest.approx(1.0)
    approval = t1.children[2].children[0]
    assert approval.duration == pytest.approx(2.0)
    assert approval.metadata["approved"] is True and approval.metadata["decided_by"] == "human"
    assert t1.children[2].duration == pytest.approx(2.5)
    assert route.start == pytest.approx(1000.0) and route.duration == pytest.approx(0.3)

    # Status: revise and escalation are warnings; the turn closes on ``stop``.
    critic = t2.children[2]
    assert critic.status == "warning" and critic.metadata["verdict"] == "revise"
    assert t3.end is not None and t3.end < writer.end  # type: ignore[operator]
    assert all(s.end is not None for s in root.walk())


def test_raw_loop_events_pair_tool_results_fifo_and_open_implicit_turns() -> None:
    """CLI / eval feed the loop's events untranslated: no ids, ``is_error``, maybe no turns."""
    clock = Clock()
    rec = TraceRecorder(clock=clock)
    rec.on_event("llm_call", llm_event("agent"))  # no ``turn`` event: opens turn 1 itself
    rec.on_event("tool_call", {"name": "run_sql", "input": {"query": "a"}})
    rec.on_event("tool_call", {"name": "run_sql", "input": {"query": "b"}})
    rec.on_event("tool_result", {"name": "run_sql", "is_error": False, "summary": "A"})
    rec.on_event("tool_result", {"name": "run_sql", "is_error": True, "summary": "B failed"})
    rec.on_event("llm_call", llm_event("agent"))
    rec.on_event("tool_call", {"name": "make_chart", "input": {}})  # never answered
    root = rec.finish(status="failed", error="boom")

    t1, t2 = root.children
    assert [c.input for c in t1.children[1:]] == [{"query": "a"}, {"query": "b"}]
    assert [c.output for c in t1.children[1:]] == ["A", "B failed"]
    assert [c.status for c in t1.children[1:]] == ["ok", "error"]
    assert t2.name == "turn 2"
    assert t2.children[1].status == "warning"  # closed by finish()
    assert root.status == "error" and root.status_message == "boom"


def test_broken_sink_never_breaks_the_run(caplog: pytest.LogCaptureFixture) -> None:
    class Broken:
        def span_started(self, span: Span, parent: Span | None) -> None:
            raise RuntimeError("down")

        span_ended = close = span_started  # type: ignore[assignment]

    rec = TraceRecorder(sinks=[Broken()])  # type: ignore[list-item]
    rec.on_event("llm_call", llm_event("agent"))
    rec.on_event("not_an_event", {})
    root = rec.finish(status="completed")
    assert root.children[0].children[0].kind == "llm_call"
    assert "trace sink Broken" in caplog.text
    assert rec.finish(status="completed") is root  # idempotent


# --------------------------------------------------------------------------- Langfuse mirror


def test_langfuse_sink_mirrors_tree_with_metadata() -> None:
    client = FakeLangfuse()
    clock = Clock()
    rec = TraceRecorder(
        sinks=[LangfuseSink(client, trace_id="a" * 32)],
        metadata={"job_id": "job_1"},
        clock=clock,
    )
    scripted_run(rec, clock)

    [root] = client.roots
    assert root.trace_id == "a" * 32 and rec.root.metadata["langfuse_trace_id"] == "a" * 32
    assert lf_shape(root) == (
        "agent",
        "analyst-run",
        [
            ("chain", "route", [("generation", "llm:router")]),
            (
                "span",
                "turn 1",
                [
                    ("generation", "llm:agent"),
                    ("span", "plan"),
                    ("tool", "tool:run_python", [("guardrail", "approval:run_python")]),
                ],
            ),
            (
                "span",
                "turn 2",
                [
                    ("generation", "llm:agent"),
                    ("tool", "tool:final_report"),
                    ("evaluator", "critic", [("generation", "llm:critic")]),
                    ("span", "escalation:L1->L2"),
                ],
            ),
            ("span", "turn 3", [("generation", "llm:agent")]),
            ("generation", "llm:report_writer"),
        ],
    )
    assert all(o.ended for o in client.log)
    assert client.flushed == 1

    gen = root.find("llm:agent")[0]
    assert gen.kwargs["model"] == M1
    assert gen.kwargs["usage_details"] == {
        "input": 100,
        "output": 50,
        "cache_read_input_tokens": 10,
    }
    assert gen.kwargs["cost_details"] == {"total": 0.02}
    assert gen.kwargs["version"] == prompt_versions()["planner"]["version"]
    assert gen.metadata["tier"] == "L1" and gen.metadata["prompt"] == "planner"

    md = root.metadata
    assert md["cost_usd"] == pytest.approx(0.2001)
    assert md["routed_tier"] == "L1" and md["final_tier"] == "L2"
    assert md["job_id"] == "job_1" and md["run_id"] == "run_x"
    assert "planner" in md["prompt_versions"]
    assert root.find("turn 2")[0].metadata["cost_usd"] == pytest.approx(0.07)
    assert root.find("critic")[0].level == "WARNING"
    assert root.find("route")[0].metadata["tier"] == "L1"


def test_real_loop_run_produces_the_same_tree(ctx: Any, settings: Settings) -> None:
    """AgentLoop + EventedLLM + recorder, end to end with the scripted FakeLLM."""
    routed = settings.model_copy(
        update={
            "routing_enabled": True,
            "router_model": ROUTER,
            "model_l1": M1,
            "model_l2": M2,
            "approval_enabled": True,
            "approval_python": True,
            "approval_row_threshold": None,
            "approval_budget_fraction": None,
        }
    )
    report = tool_use("final_report", markdown="total 350", key_numbers={"total": 350})
    fake = FakeLLM(
        router=[resp(text('{"tier": "L1", "reason": "one sum"}'), model=ROUTER)],
        agent=[
            resp(text("<plan>sum it</plan>"), tool_use("run_python", code="print(1)"), model=M1),
            resp(report, model=M1),
            resp(dict(report, id="toolu_r2"), model=M1),
            resp(dict(report, id="toolu_r3"), model=M2),
        ],
        critic=[critic_json("revise", ["x"]), critic_json("revise", ["y"]), critic_json("pass")],
    )
    client = FakeLangfuse()
    rec = TraceRecorder(sinks=[LangfuseSink(client)], question="total?")
    loop = AgentLoop(
        EventedLLM(fake, rec.on_event, cost_fn=estimate_cost_usd),
        build_default_registry(),
        ctx,
        routed,
        on_event=rec.on_event,
        approver=AutoApprover(),
    )
    result = loop.run("total?", run_id="run_test")
    root = rec.finish(status=result.status.value, output=result.report)

    assert result.status is RunStatus.COMPLETED
    assert root.metadata["run_id"] == result.state.run_id == "run_test"
    assert result.to_dict()["run_id"] == "run_test"
    kinds = [c.kind for c in root.children]
    assert kinds == ["route", "turn", "turn", "turn", "turn"]
    t1 = root.children[1]
    assert [c.kind for c in t1.children] == ["llm_call", "plan", "tool_call"]
    assert t1.children[2].children[0].kind == "approval"
    assert [c.kind for c in root.children[2].children] == ["llm_call", "tool_call", "critic"]
    # Second revise escalates L1 -> L2; the 4th turn runs on the stronger model.
    assert root.children[3].children[-1].kind == "escalation"
    assert root.children[4].metadata["tier"] == "L2"
    assert root.children[4].children[0].metadata["model"] == M2
    # Cost on the tree equals the loop's own cost tracker.
    assert root.total_cost() == pytest.approx(result.state.cost.usd)
    assert root.metadata["llm_calls"] == result.state.cost.calls == 8
    # And Langfuse got the same shape.
    [lf_root] = client.roots
    assert [c.as_type for c in lf_root.children] == ["chain", "span", "span", "span", "span"]
    assert lf_root.metadata["routed_tier"] == "L1" and lf_root.metadata["final_tier"] == "L2"


# --------------------------------------------------------------------------- persistence + CLI


def test_save_find_load_and_render(tmp_path: Path) -> None:
    clock = Clock()
    rec = TraceRecorder(metadata={"job_id": "job_1"}, question="q?", clock=clock)
    scripted_run(rec, clock)
    path = save_trace(tmp_path / "traces", rec.root, name="job_1")
    doc = json.loads(path.read_text())
    assert doc["run_id"] == "run_x" and doc["job_id"] == "job_1"
    assert doc["spans"]["cost_usd"] == pytest.approx(0.2001)

    # Lookup by file stem, by run id inside the file, by path; unknown -> None.
    assert find_trace("job_1", [tmp_path / "traces"]) == path
    assert find_trace("run_x", [tmp_path / "nope", tmp_path]) == path
    assert find_trace(str(path), []) == path
    assert find_trace("run_missing", [tmp_path]) is None

    root = load_trace(path)
    assert shape(root) == shape(rec.root)
    out = render_tree(root)
    lines = out.splitlines()
    assert lines[0].startswith("✓ run run_x job_id=job_1 status=partial tier=L1→L2")
    assert any("├─ ✓ route L1 → claude-sonnet-5-5" in ln for ln in lines)
    assert any("│     └─ ✓ approval:run_python approved by human" in ln for ln in lines)
    assert any("! critic round=1 verdict=revise" in ln and "$0.0400" in ln for ln in lines)
    assert any("llm:agent claude-sonnet-5-5 [L1] in=100 out=50" in ln for ln in lines)
    assert "total 7.00s | $0.2001 | 6 LLM calls | 2 tool calls" in out
    assert "planner@" in out
    assert "─ ✓ plan " in out and "─ ✓ plan " not in render_tree(root, show_instant=False)


def test_load_trace_accepts_ask_trace_output_and_rejects_other_json(tmp_path: Path) -> None:
    rec = TraceRecorder()
    rec.finish(status="completed")
    ask_style = tmp_path / "ask.json"
    ask_style.write_text(json.dumps({"status": "completed", "spans": rec.root.to_dict()}))
    assert load_trace(ask_style).kind == "run"
    other = tmp_path / "meta.json"
    other.write_text(json.dumps({"n_tasks": 3}))
    with pytest.raises(ValueError, match="no 'spans'"):
        load_trace(other)
