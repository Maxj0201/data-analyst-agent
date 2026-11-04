from __future__ import annotations

from typing import Any

import pytest

from analyst_agent.api.observability import (
    LangfuseTracerFactory,
    NoopTracerFactory,
    make_tracer_factory,
)


class FakeObservation:
    def __init__(self, log: list, **kwargs: Any) -> None:
        self.log = log
        self.kwargs = kwargs
        self.updates: list[dict] = []
        self.ended = False
        self.children: list[FakeObservation] = []
        log.append(self)

    def start_observation(self, **kwargs: Any) -> FakeObservation:
        child = FakeObservation(self.log, **kwargs)
        self.children.append(child)
        return child

    def update(self, **kwargs: Any) -> FakeObservation:
        self.updates.append(kwargs)
        return self

    def end(self, **_: Any) -> FakeObservation:
        self.ended = True
        return self


class FakeLangfuse:
    def __init__(self) -> None:
        self.log: list[FakeObservation] = []
        self.flushed = 0
        self.closed = False

    def start_observation(self, **kwargs: Any) -> FakeObservation:
        return FakeObservation(self.log, **kwargs)

    def flush(self) -> None:
        self.flushed += 1

    def shutdown(self) -> None:
        self.closed = True


def test_every_llm_and_tool_call_becomes_a_span():
    client = FakeLangfuse()
    tracer = LangfuseTracerFactory(client)(job_id="job_1", dataset_id="ds", question="q?")
    tracer.on_event("plan", {"plan": "do it"})
    tracer.on_event(
        "llm_call",
        {"model": "claude", "purpose": "planner", "input_tokens": 10, "output_tokens": 3,
         "cache_read_tokens": 7, "usd": 0.004},
    )  # fmt: skip
    tracer.on_event("tool_call", {"id": "a", "name": "run_sql", "input": {"query": "SELECT 1"}})
    tracer.on_event("tool_call", {"id": "b", "name": "make_chart", "input": {}})
    tracer.on_event("tool_result", {"id": "a", "name": "run_sql", "ok": True, "summary": "1 row"})
    tracer.on_event("tool_result", {"id": "b", "name": "make_chart", "ok": False, "summary": "x"})
    tracer.on_event("critic", {"verdict": "revise", "issues": ["wrong join"]})
    tracer.finish(status="succeeded", output="# report")

    root = client.log[0]
    assert root.kwargs["as_type"] == "agent"
    assert root.kwargs["metadata"]["job_id"] == "job_1"
    kinds = [(c.kwargs["as_type"], c.kwargs["name"]) for c in root.children]
    assert kinds == [
        ("span", "plan"),
        ("generation", "llm:planner"),
        ("tool", "tool:run_sql"),
        ("tool", "tool:make_chart"),
        ("evaluator", "critic"),
    ]
    gen = root.children[1]
    assert gen.kwargs["model"] == "claude"
    assert gen.kwargs["usage_details"] == {"input": 10, "output": 3, "cache_read_input_tokens": 7}
    assert gen.kwargs["cost_details"] == {"total": 0.004}
    run_sql, chart = root.children[2], root.children[3]
    assert run_sql.updates[0]["output"] == "1 row" and run_sql.updates[0]["level"] == "DEFAULT"
    assert chart.updates[0]["level"] == "ERROR"
    assert all(o.ended for o in client.log)
    assert root.updates[-1]["output"] == "# report"
    assert client.flushed == 1


def test_tracer_errors_never_propagate():
    class Broken(FakeLangfuse):
        def start_observation(self, **kwargs):
            raise RuntimeError("langfuse down")

    tracer = LangfuseTracerFactory(Broken())(job_id="j", dataset_id="d", question="q")
    tracer.on_event("plan", {})  # NoopTracer fallback, no exception
    tracer.finish(status="failed")


def test_feature_flag(monkeypatch: pytest.MonkeyPatch):
    assert isinstance(make_tracer_factory(False), NoopTracerFactory)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("LANGFUSE_HOST", "http://127.0.0.1:9")
    factory = make_tracer_factory(True)
    assert isinstance(factory, LangfuseTracerFactory)
    factory.shutdown()
