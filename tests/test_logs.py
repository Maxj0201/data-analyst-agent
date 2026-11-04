"""Structured logging: JSON lines with run_id / job_id correlation (core/logs.py)."""

from __future__ import annotations

import io
import json
import logging
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from analyst_agent.core.logs import configure_logging, current_context, log_context
from analyst_agent.core.loop import AgentLoop
from analyst_agent.tools import ToolContext, build_default_registry, create_sandbox
from tests.conftest import FakeLLM, critic_json, resp, text, tool_use


@pytest.fixture
def json_logs() -> Iterator[io.StringIO]:
    root = logging.getLogger()
    saved = (list(root.handlers), root.level)
    buf = io.StringIO()
    configure_logging(json_logs=True, level="INFO", stream=buf)
    yield buf
    for h in list(root.handlers):
        if h not in saved[0]:
            root.removeHandler(h)
    root.setLevel(saved[1])


def lines(buf: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(ln) for ln in buf.getvalue().splitlines() if ln.strip()]


def test_json_lines_carry_context_and_extras(json_logs: io.StringIO) -> None:
    log = logging.getLogger("analyst_agent.test")
    with log_context(job_id="job_1"):
        with log_context(run_id="run_1", ignored=None):
            log.info("hello %s", "world", extra={"rows": 3})
        log.warning("outer")
    log.error("bare")
    try:
        raise ValueError("bad")
    except ValueError:
        log.exception("failed")

    a, b, c, d = lines(json_logs)
    assert a["msg"] == "hello world" and a["level"] == "info"
    assert a["logger"] == "analyst_agent.test" and a["ts"].endswith("Z")
    assert (a["job_id"], a["run_id"], a["rows"]) == ("job_1", "run_1", 3)
    assert "ignored" not in a
    assert b["job_id"] == "job_1" and "run_id" not in b
    assert "job_id" not in c
    assert "ValueError: bad" in d["exc_info"]


def test_configure_logging_is_idempotent_and_text_mode_shows_context() -> None:
    root = logging.getLogger()
    saved = (list(root.handlers), root.level)
    try:
        configure_logging(json_logs=True, level="INFO", stream=io.StringIO())
        buf = io.StringIO()
        configure_logging(json_logs=False, level="DEBUG", stream=buf)
        ours = [h for h in root.handlers if getattr(h, "_analyst_agent_handler", False)]
        assert len(ours) == 1 and root.level == logging.DEBUG
        with log_context(job_id="job_9"):
            logging.getLogger("x").debug("text line", extra={"k": "v"})
        out = buf.getvalue()
        assert "text line [job_id=job_9 k=v]" in out
    finally:
        for h in list(root.handlers):
            if h not in saved[0]:
                root.removeHandler(h)
        root.setLevel(saved[1])


def test_context_is_isolated_between_threads() -> None:
    seen: dict[str, dict[str, Any]] = {}
    barrier = threading.Barrier(2)

    def work(job: str) -> None:
        with log_context(job_id=job):
            barrier.wait(timeout=5)  # both contexts are bound at the same time
            seen[job] = current_context()

    threads = [threading.Thread(target=work, args=(j,)) for j in ("job_a", "job_b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen == {"job_a": {"job_id": "job_a"}, "job_b": {"job_id": "job_b"}}
    assert current_context() == {}


def test_loop_and_sandbox_lines_share_run_and_job_id(json_logs, sales_csv, settings) -> None:
    llm = FakeLLM(
        agent=[
            resp(text("<plan>sum</plan>"), tool_use("run_sql", query="SELECT 1 AS x")),
            resp(tool_use("final_report", markdown="x is 1", key_numbers={"x": 1})),
        ],
        critic=[critic_json("pass")],
    )
    with create_sandbox({"sales": sales_csv}, artifacts_dir=settings.artifacts_dir) as sb:
        ctx = ToolContext(tables=sb.tables, artifacts_dir=settings.artifacts_dir, sandbox=sb)
        with log_context(job_id="job_7"):
            result = AgentLoop(llm, build_default_registry(), ctx, settings).run("q", run_id="r1")

    recs = [r for r in lines(json_logs) if r["logger"].startswith("analyst_agent")]
    by_msg = {r["msg"]: r for r in recs}
    assert {"run started", "tool call", "sandbox sql", "run finished"} <= set(by_msg)
    for r in recs:
        assert (r.get("job_id"), r.get("run_id")) == ("job_7", "r1"), r
    assert by_msg["sandbox sql"]["ok"] is True and by_msg["sandbox sql"]["row_count"] == 1
    assert by_msg["run finished"]["status"] == result.status.value == "completed"
