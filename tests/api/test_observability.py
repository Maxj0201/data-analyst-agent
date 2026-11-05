from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import httpx
import pytest

pytest.importorskip("fastapi", reason="needs the 'api' extra: uv sync --extra api")

import tests.conftest as core_conftest  # noqa: E402
from analyst_agent.api.app import create_app  # noqa: E402
from analyst_agent.api.observability import (  # noqa: E402
    LangfuseTracerFactory,
    NoopTracerFactory,
    RecorderTracerFactory,
    make_tracer_factory,
)
from analyst_agent.core.config import Settings  # noqa: E402
from analyst_agent.core.logs import configure_logging  # noqa: E402
from analyst_agent.core.tracing import find_trace, load_trace  # noqa: E402
from tests.fake_langfuse import FakeLangfuse  # noqa: E402

from .conftest import upload, wait_for_job  # noqa: E402


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
    assert root.kwargs["as_type"] == "agent" and root.name == "analyst-job"
    assert root.metadata["job_id"] == "job_1"
    assert root.kwargs["input"] == {"question": "q?"}
    kinds = [(c.as_type, c.name) for c in root.children]
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
    assert run_sql.updates[0]["output"] == "1 row" and run_sql.level == "DEFAULT"
    assert chart.level == "ERROR"
    assert all(o.ended for o in client.log)
    assert root.updates[-1]["output"] == "# report"
    assert root.metadata["cost_usd"] == 0.004 and root.metadata["status"] == "succeeded"
    assert client.flushed == 1


def test_tracer_errors_never_propagate():
    class Broken(FakeLangfuse):
        def start_observation(self, **kwargs):
            raise RuntimeError("langfuse down")

    tracer = LangfuseTracerFactory(Broken())(job_id="j", dataset_id="d", question="q")
    tracer.on_event("plan", {})  # sink failures are logged by the recorder, never raised
    tracer.finish(status="failed")


def test_feature_flags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    assert isinstance(make_tracer_factory(False), NoopTracerFactory)
    store_only = make_tracer_factory(False, store_dir=tmp_path)
    assert isinstance(store_only, RecorderTracerFactory) and not store_only.langfuse_enabled
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("LANGFUSE_HOST", "http://127.0.0.1:9")
    factory = make_tracer_factory(True)
    assert isinstance(factory, LangfuseTracerFactory) and factory.langfuse_enabled
    factory.shutdown()


@pytest.mark.asyncio
async def test_real_job_traces_logs_and_saved_tree(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Real loop behind the API: Langfuse tree, saved trace and correlated JSON logs."""
    import analyst_agent.core as core_pkg

    h = core_conftest
    monkeypatch.setenv("AGENT_ARTIFACTS_DIR", str(settings.artifacts_dir))
    fake = h.FakeLLM(
        agent=[
            h.resp(
                h.text("<plan>sum revenue</plan>"),
                h.tool_use("run_sql", query="SELECT SUM(revenue) AS t FROM s"),
            ),
            h.resp(h.tool_use("final_report", markdown="Revenue is 350", key_numbers={"t": 350})),
        ],
        critic=[h.critic_json("pass")],
    )
    monkeypatch.setattr(core_pkg, "LLMClient", lambda settings: fake)
    lf = FakeLangfuse()
    traces = tmp_path / "traces"
    root_logger = logging.getLogger()
    saved_handlers = list(root_logger.handlers)
    buf = io.StringIO()
    configure_logging(json_logs=True, level="INFO", stream=buf)
    try:
        app = create_app(settings, tracer_factory=LangfuseTracerFactory(lf, store_dir=traces))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            dataset_id = await upload(c)
            resp = await c.post("/ask", json={"dataset_id": dataset_id, "question": "Total?"})
            job_id = resp.json()["job_id"]
            job = await wait_for_job(c, job_id, timeout=20)
        # The job turns terminal before the worker saves the trace and logs "job finished":
        # drain the worker so those assertions don't race it.
        app.state.jobs._executor.shutdown(wait=True)
        app.state.jobs.shutdown()
    finally:
        for hd in list(root_logger.handlers):
            if hd not in saved_handlers:
                root_logger.removeHandler(hd)
    assert job["status"] == "succeeded", job

    # Langfuse: run -> turn -> llm / tool / critic, with run + job ids and cost metadata.
    [root] = lf.roots
    assert [(c.as_type, c.name) for c in root.children] == [("span", "turn 1"), ("span", "turn 2")]
    t1, t2 = root.children
    assert [c.as_type for c in t1.children] == ["generation", "span", "tool"]
    assert [c.as_type for c in t2.children] == ["generation", "tool", "evaluator"]
    assert t2.children[2].children[0].name == "llm:critic"
    md = root.metadata
    assert md["job_id"] == job_id and md["run_id"].startswith("run_")
    assert md["llm_calls"] == 3 and md["cost_usd"] == pytest.approx(job["cost"]["usd"])
    assert "planner" in md["prompt_versions"]

    # Saved tree, findable by job id and by run id.
    path = find_trace(job_id, [traces])
    assert path == traces / f"{job_id}.json"
    assert find_trace(md["run_id"], [traces]) == path
    assert [c.name for c in load_trace(path).children] == ["turn 1", "turn 2"]

    # JSON logs: API, loop and sandbox lines for this job share job_id and run_id.
    recs = [json.loads(ln) for ln in buf.getvalue().splitlines() if ln.startswith("{")]
    ours = [r for r in recs if r.get("job_id") == job_id]
    loggers = {r["logger"] for r in ours}
    assert {"analyst_agent.api.jobs", "analyst_agent.core.loop", "analyst_agent.sandbox"} <= loggers
    in_run = [
        r for r in ours if r["logger"] in ("analyst_agent.core.loop", "analyst_agent.sandbox")
    ]
    assert {r.get("run_id") for r in in_run} == {md["run_id"]}
    assert any(r["msg"] == "job finished" and r["status"] == "succeeded" for r in ours)
