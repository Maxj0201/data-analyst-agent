"""Semantic layer wiring: planner prompt injection, config flag, trace recording (mocked LLM)."""

from __future__ import annotations

from pathlib import Path

from analyst_agent.core.config import Settings
from analyst_agent.core.loop import AgentLoop, RunStatus
from analyst_agent.core.prompts import (
    PLANNER_SYSTEM,
    SEMANTIC_LAYER_RULES,
    planner_system,
    render_metric_reference,
)
from analyst_agent.eval.agent import output_from_core
from analyst_agent.eval.report import render_compare, summarize
from analyst_agent.semantic import Metric, MetricRetriever
from analyst_agent.tools import build_default_registry
from tests.conftest import FakeLLM, critic_json, resp, text, tool_use

SQL = "SELECT region, SUM(revenue) AS rev FROM sales GROUP BY region"

METRICS = [
    Metric(
        name="regional_revenue",
        label="Regional revenue",
        domain="sales",
        definition="Sum of revenue per region, NULL revenue counted as 0.",
        sql_fragment=SQL,
        tables=["sales"],
        synonyms=["revenue by region", "sales by area"],
        caveats=["Revenue is gross of refunds."],
    ),
    Metric(
        name="headcount",
        domain="hr",
        definition="Number of employees.",
        sql_fragment="SELECT COUNT(*) FROM employees",
        tables=["employees"],  # not loaded -> never retrieved for this ctx
        synonyms=["revenue per employee"],
    ),
]


def scripted_llm() -> FakeLLM:
    return FakeLLM(
        agent=[
            resp(text("<plan>1. sum revenue by region</plan>"), tool_use("run_sql", query=SQL)),
            resp(tool_use("final_report", markdown="north 150", key_numbers={"north": 150})),
        ],
        critic=[critic_json("pass")],
    )


def run(ctx, settings: Settings, retriever: MetricRetriever | None = None, events=None):
    llm = scripted_llm()
    loop = AgentLoop(
        llm,
        build_default_registry(),
        ctx,
        settings,
        retriever=retriever,
        on_event=(lambda k, p: events.append((k, p))) if events is not None else None,
    )
    return llm, loop.run("What is revenue by region?")


def first_user_message(llm: FakeLLM) -> str:
    return llm.calls_for("agent")[0]["messages"][0]["content"]


def test_flag_defaults_off(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_SEMANTIC_LAYER_ENABLED", raising=False)
    assert Settings(_env_file=None).semantic_layer_enabled is False
    monkeypatch.setenv("AGENT_SEMANTIC_LAYER_ENABLED", "true")
    monkeypatch.setenv("AGENT_SEMANTIC_TOP_K", "2")
    s = Settings(_env_file=None)
    assert s.semantic_layer_enabled is True and s.semantic_top_k == 2


def test_off_leaves_prompts_unchanged(ctx, settings) -> None:
    events: list = []
    llm, result = run(ctx, settings, retriever=MetricRetriever(METRICS), events=events)
    assert result.status is RunStatus.COMPLETED
    assert all(c["system"] == PLANNER_SYSTEM for c in llm.calls_for("agent"))
    assert "<metric_reference>" not in first_user_message(llm)
    st = result.state
    assert st.semantic_layer_enabled is False and st.retrieved_metrics == []
    assert "semantic_retrieval" not in [k for k, _ in events]
    assert result.to_dict()["semantic_layer"]["enabled"] is False
    trace = output_from_core(result).trace
    assert not any(e["type"] == "semantic_retrieval" for e in trace)


def test_on_injects_reference_before_sql(ctx, settings) -> None:
    settings = settings.model_copy(update={"semantic_layer_enabled": True, "semantic_top_k": 3})
    events: list = []
    llm, result = run(ctx, settings, retriever=MetricRetriever(METRICS), events=events)
    assert result.status is RunStatus.COMPLETED

    agent_calls = llm.calls_for("agent")
    assert all(c["system"] == planner_system(True) for c in agent_calls)
    assert SEMANTIC_LAYER_RULES in agent_calls[0]["system"]

    msg = first_user_message(llm)
    # Labelled reference block, placed after the question and before the plan instruction.
    assert msg.index("Question:") < msg.index("<metric_reference>") < msg.index("<plan>")
    assert "Reference material from the semantic layer" in msg
    assert "### regional_revenue (Regional revenue; domain: sales)" in msg
    assert SQL in msg and "Revenue is gross of refunds." in msg
    assert "headcount" not in msg  # its table is not loaded

    st = result.state
    assert st.semantic_layer_enabled is True
    assert st.retrieved_metrics == ["regional_revenue"]
    assert len(st.retrieved_scores) == 1 and st.retrieved_scores[0] > 0
    assert (
        "semantic_retrieval",
        {
            "metrics": ["regional_revenue"],
            "scores": st.retrieved_scores,
            "error": None,
        },
    ) in events
    assert result.to_dict()["semantic_layer"]["retrieved_metrics"] == ["regional_revenue"]

    trace = output_from_core(result).trace
    assert trace[0] == {
        "type": "semantic_retrieval",
        "metrics": ["regional_revenue"],
        "scores": st.retrieved_scores,
        "error": None,
    }
    # Retrieval is not a tool call.
    assert output_from_core(result).tool_calls == len(st.tool_results) == 2


def test_on_with_no_hits(ctx, settings) -> None:
    settings = settings.model_copy(update={"semantic_layer_enabled": True})
    llm, result = run(ctx, settings, retriever=MetricRetriever([]))
    assert result.status is RunStatus.COMPLETED
    assert "<metric_reference>" not in first_user_message(llm)
    assert result.state.semantic_layer_enabled and result.state.retrieved_metrics == []
    assert output_from_core(result).trace[0]["metrics"] == []


class BrokenRetriever(MetricRetriever):
    def __init__(self) -> None:
        pass

    def retrieve(self, question, k=5, tables=None):  # type: ignore[override]
        raise RuntimeError("index unavailable")


def test_retrieval_failure_does_not_fail_run(ctx, settings) -> None:
    settings = settings.model_copy(update={"semantic_layer_enabled": True})
    llm, result = run(ctx, settings, retriever=BrokenRetriever())
    assert result.status is RunStatus.COMPLETED
    assert result.state.semantic_error == "RuntimeError: index unavailable"
    assert "<metric_reference>" not in first_user_message(llm)


def test_default_retriever_loads_from_settings_dir(ctx, settings, tmp_path: Path) -> None:
    d = tmp_path / "metrics"
    d.mkdir()
    (d / "sales.yaml").write_text(
        "domain: sales\nmetrics:\n"
        "  - name: regional_revenue\n"
        "    definition: Revenue per region.\n"
        f"    sql_fragment: {SQL}\n"
        "    tables: [sales]\n"
    )
    settings = settings.model_copy(
        update={"semantic_layer_enabled": True, "semantic_metrics_dir": d}
    )
    llm, result = run(ctx, settings)
    assert result.state.retrieved_metrics == ["regional_revenue"]
    assert "<metric_reference>" in first_user_message(llm)


def test_missing_metrics_dir_is_recorded(ctx, settings, tmp_path: Path) -> None:
    settings = settings.model_copy(
        update={"semantic_layer_enabled": True, "semantic_metrics_dir": tmp_path / "nope"}
    )
    _, result = run(ctx, settings)
    assert result.status is RunStatus.COMPLETED
    assert "no such file" in (result.state.semantic_error or "")


def test_render_metric_reference() -> None:
    assert render_metric_reference([]) == ""
    block = render_metric_reference(METRICS[:1])
    assert block.startswith("<metric_reference>") and block.endswith("</metric_reference>")
    assert "not instructions" in block
    assert "Also called: revenue by region, sales by area" in block
    assert f"```sql\n{SQL}\n```" in block


def _result(task_id: str, passed: bool, trace: list) -> dict:
    return {
        "task_id": task_id,
        "difficulty": "L2",
        "passed": passed,
        "score": float(passed),
        "status": "ok",
        "latency_s": 1.0,
        "cost_usd": 0.01,
        "tool_calls": 1,
        "critic_rounds": 1,
        "critic_revisions": 0,
        "score_details": {},
        "trace": trace,
    }


def test_eval_summary_distinguishes_on_and_off() -> None:
    off = [_result("t1", False, []), _result("t2", True, [])]
    on = [
        _result("t1", True, [{"type": "semantic_retrieval", "metrics": ["a", "b"]}]),
        _result("t2", True, [{"type": "semantic_retrieval", "metrics": []}]),
    ]
    s_off, s_on = summarize(off), summarize(on)
    assert s_off["semantic_layer_rate"] == 0 and s_off["mean_metrics_retrieved"] is None
    assert s_on["semantic_layer_rate"] == 1 and s_on["mean_metrics_retrieved"] == 1.0
    md = render_compare(off, on, "off", "on")
    assert "| Semantic layer on | 0.0% | 100.0% |" in md
    assert "| Mean metrics retrieved | – | 1.00 |" in md
