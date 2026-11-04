"""Failure-path branches of the agent loop: observers, critic and report writer that raise,
memory with nothing to save, and the step-summary fallback."""

from __future__ import annotations

from typing import Any

from analyst_agent.core.loop import (
    AgentLoop,
    AgentState,
    RunStatus,
    ToolCallRecord,
    _steps_summary,
)
from analyst_agent.core.memory import InsightStore
from analyst_agent.tools import build_default_registry
from tests.conftest import FakeLLM, critic_json, resp, tool_use

SQL = "SELECT region, SUM(revenue) AS rev FROM sales GROUP BY region ORDER BY region"


def make_loop(llm, ctx, settings, **kw):
    return AgentLoop(llm, build_default_registry(), ctx, settings, **kw)


def _answer(**report: Any) -> list[Any]:
    return [
        resp(tool_use("run_sql", query=SQL)),
        resp(tool_use("final_report", **report)),
    ]


def _rec(name: str, args: dict[str, Any], output: str) -> ToolCallRecord:
    return ToolCallRecord(
        id=f"t_{name}", name=name, input=args, output=output, is_error=False, turn=1
    )


def test_raising_event_hook_does_not_break_the_run(ctx, settings) -> None:
    def hook(kind: str, payload: dict[str, Any]) -> None:
        raise RuntimeError("observer bug")

    llm = FakeLLM(
        agent=_answer(markdown="north 150", key_numbers={"north": 150}),
        critic=[critic_json("pass")],
    )
    result = make_loop(llm, ctx, settings, on_event=hook).run("Revenue?")
    assert result.status is RunStatus.COMPLETED


def test_critic_exception_accepts_report_unverified(ctx, settings) -> None:
    class BrokenCritic:
        def review(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("critic down")

    llm = FakeLLM(agent=_answer(markdown="north 150", key_numbers={"north": 150}))
    result = make_loop(llm, ctx, settings, critic=BrokenCritic()).run("Revenue?")
    assert result.status is RunStatus.COMPLETED_WITH_ISSUES
    assert "Critic unavailable" in result.state.detail
    assert result.state.critic_reviews[0]["parse_error"] == "critic call failed: critic down"
    assert result.report.endswith("north 150")


def test_report_writer_failure_falls_back_to_step_summary(ctx, settings) -> None:
    settings.max_turns = 1
    llm = FakeLLM(
        agent=[resp(tool_use("run_sql", query=SQL))], writer=[]
    )  # empty queue: writer raises
    result = make_loop(llm, ctx, settings).run("Revenue?")
    assert result.status is RunStatus.MAX_TURNS
    assert "## Partial results" in result.report
    assert SQL in result.report


def test_memory_skips_save_without_fingerprint_or_content(
    ctx, settings, tmp_path, monkeypatch
) -> None:
    import analyst_agent.core.loop as loop_mod

    monkeypatch.setattr(loop_mod, "insight_from_result", lambda *a, **k: None)
    settings.memory_enabled = True
    settings.memory_db_path = tmp_path / "mem.sqlite3"
    store = InsightStore(settings.memory_db_path)
    events: list[tuple[str, dict[str, Any]]] = []
    # insight_from_result found nothing worth keeping.
    llm = FakeLLM(agent=_answer(markdown="ok", key_numbers={}), critic=[critic_json("pass")])
    agent = make_loop(llm, ctx, settings, memory=store, on_event=lambda k, p: events.append((k, p)))
    res = agent.run("Revenue?")
    assert res.status is RunStatus.COMPLETED
    assert not res.state.memory_saved and res.state.memory_error is None
    assert ("memory_saved", {"saved": False, "error": None}) in events

    loop = make_loop(FakeLLM(agent=[]), ctx, settings, memory=store)
    state = AgentState(question="q", tables=["sales"])
    loop._remember(state)  # no fingerprint (retrieval never ran): silently does nothing
    assert not state.memory_saved


def test_steps_summary_shapes() -> None:
    state = AgentState(question="q", tables=["sales"])
    assert "No analysis steps completed" in _steps_summary(state)
    state.plan = ["1. look"]
    state.tool_results = [
        _rec("inspect_schema", {}, "tables: sales"),
        _rec("run_sql", {"query": "SELECT 1"}, "x" * 3000),
    ]
    out = _steps_summary(state)
    assert "### Plan\n1. look" in out
    assert "**inspect_schema**\ntables: sales" in out
    assert "```\nSELECT 1\n```" in out and "[truncated]" in out


def test_synthesize_skips_terminal_tool(ctx, settings) -> None:
    llm = FakeLLM(agent=[], writer=[resp()])
    loop = make_loop(llm, ctx, settings)
    state = AgentState(question="q", tables=["sales"], model=settings.model)
    state.tool_results = [
        _rec("run_sql", {"query": "SELECT 1"}, "1"),
        _rec("final_report", {"markdown": "SECRET DRAFT"}, "ok"),
    ]
    loop._synthesize(state)
    content = llm.calls_for("report_writer")[0]["messages"][0]["content"]
    assert "SELECT 1" in content and "SECRET DRAFT" not in content
