"""frontend/ui_helpers.py: timeline rows, per-model costs and report splitting."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "frontend"))

from ui_helpers import (  # noqa: E402
    iter_sse,
    model_costs,
    scalar_numbers,
    split_report,
    step_view,
)


def test_step_view_covers_every_streamed_step() -> None:
    assert step_view("plan", {"plan": "1. look"}).kind == "plan"
    route = step_view("route", {"tier": "L2", "model": "m2", "source": "fallback"})
    assert "Routed to L2" in route.title and "fallback" in route.title
    sem = step_view("semantic_retrieval", {"metrics": ["mrr", "churn_rate"]})
    assert "`mrr`" in sem.title and sem.kind == "metrics"
    call = step_view("tool_call", {"name": "run_sql", "input": {"query": "SELECT 1"}})
    assert (call.code, call.lang) == ("SELECT 1", "sql")
    py = step_view("tool_call", {"name": "run_python", "input": {"code": "print(1)"}})
    assert py.lang == "python"
    assert step_view("tool_call", {"name": "final_report", "input": {"markdown": "x"}}).code == ""
    chart = step_view("tool_call", {"name": "make_chart", "input": {"spec": {"x": [1]}}})
    assert chart.code == "" and "spec" in chart.more
    bad = step_view("tool_result", {"name": "run_sql", "ok": False, "summary": "boom"})
    assert bad.tone == "error" and "boom" in bad.title
    critic = step_view("critic", {"verdict": "revise", "issues": ["wrong join"]})
    assert critic.tone == "warn" and critic.bullets == ["wrong join"]
    gate = step_view("approval_required", {"summary": "big scan", "reasons": ["1e9 rows"]})
    assert gate.kind == "approval" and gate.bullets == ["1e9 rows"]
    denied = step_view(
        "approval_decision",
        {"tool": "run_sql", "approved": False, "decided_by": "human", "note": "no"},
    )
    assert denied.title == "run_sql denied (human) — no" and denied.tone == "error"
    assert step_view("llm_call", {"model": "m"}) is None
    assert step_view("unknown", {}) is None


def test_iter_sse_parses_until_end() -> None:
    lines = [
        "retry: 2000",
        "",
        "id: 1",
        "event: plan",
        'data: {"seq": 1, "plan": "p"}',
        "",
        ": keep-alive",
        "",
        "event: end",
        'data: {"status": "succeeded"}',
        "",
        "event: plan",
        'data: {"seq": 9}',
        "",
    ]
    assert list(iter_sse(lines)) == [
        ("plan", {"seq": 1, "plan": "p"}),
        ("end", {"status": "succeeded"}),
    ]


def test_model_costs_groups_by_model() -> None:
    events = [
        ("llm_call", {"model": "big", "purpose": "agent", "input_tokens": 100, "usd": 0.5}),
        ("tool_call", {"name": "run_sql"}),
        ("llm_call", {"model": "small", "purpose": "router", "input_tokens": 10, "usd": 0.01}),
        ("llm_call", {"model": "big", "purpose": "critic", "output_tokens": 7, "usd": 0.25}),
    ]
    rows = model_costs(events)
    assert [r["model"] for r in rows] == ["big", "small"]
    big = rows[0]
    assert big["calls"] == 2 and big["input_tokens"] == 100 and big["output_tokens"] == 7
    assert big["usd"] == 0.75 and big["purposes"] == "agent, critic"


def test_split_report_keeps_charts_in_place() -> None:
    md = '# T\n\nIntro.\n\n![rev](/artifacts/j/a.png)\n\nMore.\n![](/artifacts/j/b.png "t")'
    assert split_report(md) == [
        ("md", "# T\n\nIntro.\n\n", ""),
        ("img", "/artifacts/j/a.png", "rev"),
        ("md", "\n\nMore.\n", ""),
        ("img", "/artifacts/j/b.png", ""),
    ]
    assert split_report("no charts") == [("md", "no charts", "")]


def test_scalar_numbers_formats_tiles() -> None:
    out = scalar_numbers({"top_region": "NA", "total_mrr": 1234.5, "n": 3, "nested": {"a": 1}})
    assert out == [("top region", "NA"), ("total mrr", "1,234.50"), ("n", "3")]


def test_step_html_escapes_agent_text() -> None:
    from ui_helpers import Step, step_html, summarize_output

    out = step_html(Step("tool", "**run_sql** `<b>`", detail="<script>x</script>", more="a<b"))
    assert "<strong>run_sql</strong>" in out and "<code>&lt;b&gt;</code>" in out
    assert "<script>" not in out and "a&lt;b" in out
    assert summarize_output("| a |\n| --- |\n| 1 |\n| 2 |") == (
        "2 rows",
        "| a |\n| --- |\n| 1 |\n| 2 |",
    )
    assert summarize_output("3 rows") == ("3 rows", "")
    assert summarize_output("## Title\n\nbody")[0] == "Title"
