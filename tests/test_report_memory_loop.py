"""Loop wiring for AGENT_STRUCTURED_REPORT_ENABLED and AGENT_MEMORY_ENABLED (mocked LLM)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from analyst_agent.core.config import Settings
from analyst_agent.core.loop import AgentLoop, RunStatus
from analyst_agent.core.memory import InsightStore, dataset_fingerprint
from analyst_agent.core.prompts import MEMORY_RULES, PLANNER_SYSTEM, STRUCTURED_REPORT_RULES
from analyst_agent.tools import ToolContext, build_default_registry
from tests.conftest import FakeLLM, critic_json, resp, text, tool_use

SQL = "SELECT region, SUM(revenue) AS rev FROM sales GROUP BY region ORDER BY region"


def structured(**over: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "headline": "South has the most revenue (200); north has 150.",
        "key_numbers": {"south": 200, "north": 150},
        "method": "SUM(revenue) by region; NULL revenue excluded.",
        "caveats": ["One south row has NULL revenue."],
        "charts": [],
    }
    args.update(over)
    return args


def loop(llm: FakeLLM, ctx: ToolContext, settings: Settings, **kw: Any) -> AgentLoop:
    return AgentLoop(llm, build_default_registry(), ctx, settings, **kw)


def critic_inputs(llm: FakeLLM) -> list[str]:
    return [c["messages"][0]["content"] for c in llm.calls if c["role"] == "critic"]


def tool_results_of(message: dict[str, Any]) -> list[dict[str, Any]]:
    return [b for b in message["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]


# --------------------------------------------------------------------------- structured report


def test_flags_off_keep_legacy_prompt_and_schema(ctx, settings) -> None:
    llm = FakeLLM(
        agent=[resp(tool_use("final_report", markdown="# R", key_numbers={}))],
        critic=[critic_json("pass")],
    )
    result = loop(llm, ctx, settings).run("q")
    assert result.status is RunStatus.COMPLETED
    first = llm.calls[0]
    assert first["system"] == PLANNER_SYSTEM
    fr = next(t for t in first["tools"] if t["name"] == "final_report")
    assert "headline" not in fr["input_schema"]["properties"]
    d = result.to_dict()
    assert d["structured_report"] is None and d["report_checks"] == []
    assert d["memory"]["enabled"] is False


def test_structured_report_happy_path(ctx, settings) -> None:
    settings.structured_report_enabled = True
    events: list[str] = []
    spec = {"title": "Revenue by region", "x": ["north", "south"], "y": [150, 200]}
    # make_chart's output path is deterministic: <slug>-<sha1(spec)[:8]>.png
    digest = hashlib.sha1(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:8]
    chart = str(ctx.artifacts_dir / f"revenue-by-region-{digest}.png")
    llm = FakeLLM(
        agent=[
            resp(text("<plan>sum by region</plan>"), tool_use("run_sql", query=SQL)),
            resp(tool_use("make_chart", spec=spec)),
            resp(tool_use("final_report", **structured(charts=[{"path": chart, "caption": "c"}]))),
        ],
        critic=[critic_json("pass")],
    )
    lp = loop(llm, ctx, settings, on_event=lambda k, p: events.append(k))
    result = lp.run("Which region has the most revenue?")
    assert result.state.artifacts == [chart]
    assert f"![c]({chart})" in result.report
    assert result.status is RunStatus.COMPLETED, result.state.detail
    assert result.report.startswith("## Answer\nSouth has the most revenue")
    assert "## Key numbers" in result.report and "## Caveats" in result.report
    assert result.key_numbers == {"south": 200, "north": 150}
    assert result.state.structured_report is not None
    assert result.state.report_checks == [
        {"ok": True, "headline_numbers": 2, "key_numbers_checked": 2, "issues": []}
    ]
    assert "report_check" in events
    # Prompt and schema were switched to the structured variant.
    first = llm.calls[0]
    assert first["system"] == f"{PLANNER_SYSTEM}\n{STRUCTURED_REPORT_RULES}"
    fr = next(t for t in first["tools"] if t["name"] == "final_report")
    assert set(fr["input_schema"]["required"]) >= {"headline", "method", "caveats", "charts"}
    assert "Automated consistency check" not in critic_inputs(llm)[0]


def test_free_markdown_rejected_when_structured_enabled(ctx, settings) -> None:
    settings.structured_report_enabled = True
    llm = FakeLLM(
        agent=[
            resp(tool_use("run_sql", query=SQL)),
            resp(tool_use("final_report", markdown="south 200", key_numbers={"south": 200})),
            resp(tool_use("final_report", **structured())),
        ],
        critic=[critic_json("pass")],
    )
    result = loop(llm, ctx, settings).run("q")
    assert result.status is RunStatus.COMPLETED
    st = result.state
    assert [r.is_error for r in st.tool_results if r.name == "final_report"] == [True, False]
    rejected = tool_results_of(st.messages[4])[0]
    assert rejected["is_error"] and "structured fields" in rejected["content"]
    assert st.critic_rounds == 1  # the rejected submission never reached the critic


def test_unknown_chart_path_rejected(ctx, settings) -> None:
    settings.structured_report_enabled = True
    bogus = structured(charts=[{"path": "artifacts/invented.png", "caption": "x"}])
    llm = FakeLLM(
        agent=[
            resp(tool_use("run_sql", query=SQL)),
            resp(tool_use("final_report", **bogus)),
            resp(tool_use("final_report", **structured())),
        ],
        critic=[critic_json("pass")],
    )
    result = loop(llm, ctx, settings).run("q")
    errs = [r.output for r in result.state.tool_results if r.is_error]
    assert len(errs) == 1 and "artifacts/invented.png" in errs[0]
    assert result.status is RunStatus.COMPLETED


def test_unsupported_numbers_flagged_to_critic_then_revised(ctx, settings) -> None:
    settings.structured_report_enabled = True
    bad = structured(
        headline="South leads with 200, a 33% lead; total revenue is 350.",
        key_numbers={"south": 200, "north": 150, "total": 350},
    )
    llm = FakeLLM(
        agent=[
            resp(tool_use("run_sql", query=SQL)),
            resp(tool_use("final_report", **bad)),
            resp(tool_use("final_report", **structured())),
        ],
        critic=[
            critic_json("revise", issues=["total and 33% unsupported"], fix="query"),
            critic_json("pass"),
        ],
    )
    result = loop(llm, ctx, settings).run("Which region has the most revenue?")
    assert result.status is RunStatus.COMPLETED
    checks = result.state.report_checks
    assert len(checks) == 2 and not checks[0]["ok"] and checks[1]["ok"]
    kinds = {(i["kind"], i["text"]) for i in checks[0]["issues"]}
    assert ("not_in_key_numbers", "33%") in kinds
    assert ("untraced", "350") in kinds
    first, second = critic_inputs(llm)
    assert "## Automated consistency check" in first and "total" in first
    assert "Automated consistency check" not in second


def test_custom_critic_without_notes_param_still_works(ctx, settings) -> None:
    """Critics written against the old signature are only passed notes when there are any."""
    settings.structured_report_enabled = True

    class OldCritic:
        def review(self, question, draft, key_numbers, records, cost=None):  # noqa: ANN001
            from analyst_agent.core.critic import CriticVerdict

            return CriticVerdict(verdict="pass")

    llm = FakeLLM(
        agent=[resp(tool_use("run_sql", query=SQL)), resp(tool_use("final_report", **structured()))]
    )
    result = loop(llm, ctx, settings, critic=OldCritic()).run("q")
    assert result.status is RunStatus.COMPLETED


# --------------------------------------------------------------------------- memory


def _memory_settings(settings: Settings, tmp_path: Path) -> Settings:
    settings.memory_enabled = True
    settings.memory_db_path = tmp_path / "mem" / "memory.sqlite3"
    return settings


def _answer_run(
    ctx: ToolContext, settings: Settings, question: str, **kw: Any
) -> tuple[Any, FakeLLM]:
    llm = FakeLLM(
        agent=[
            resp(tool_use("run_sql", query=SQL)),
            resp(
                tool_use(
                    "final_report",
                    markdown="# Revenue\n\nSouth leads with 200.",
                    key_numbers={"south": 200},
                )
            ),
        ],
        critic=[critic_json("pass")],
    )
    return loop(llm, ctx, settings, **kw).run(question), llm


def test_memory_saves_then_retrieves_for_same_dataset(ctx, settings, tmp_path) -> None:
    s = _memory_settings(settings, tmp_path)
    events: list[tuple[str, dict[str, Any]]] = []
    first, llm1 = _answer_run(ctx, s, "Which region has the most revenue?")
    assert first.state.memory_saved and first.state.prior_insights == []
    assert "<prior_insights>" not in llm1.calls[0]["messages"][0]["content"]
    assert llm1.calls[0]["system"] == f"{PLANNER_SYSTEM}\n{MEMORY_RULES}"

    second, llm2 = _answer_run(
        ctx, s, "What is the revenue of each region?", on_event=lambda k, p: events.append((k, p))
    )
    msg = llm2.calls[0]["messages"][0]["content"]
    assert "<prior_insights>" in msg and "Prior context from earlier sessions" in msg
    assert "Question: Which region has the most revenue?" in msg
    assert "Finding: South leads with 200." in msg
    assert msg.index("Question:") < msg.index("<prior_insights>")
    assert second.state.prior_insights[0]["question"] == "Which region has the most revenue?"
    d = second.to_dict()["memory"]
    assert d["enabled"] and d["fingerprint"] == dataset_fingerprint(ctx) and d["saved"]
    retrieval = next(p for k, p in events if k == "memory_retrieval")
    assert retrieval["questions"] == ["Which region has the most revenue?"]


def test_memory_not_shared_across_datasets(ctx, settings, tmp_path) -> None:
    s = _memory_settings(settings, tmp_path)
    store = InsightStore(s.memory_db_path)
    from analyst_agent.core.memory import Insight

    store.add(Insight(fingerprint="some-other-dataset", question="revenue by region", headline="x"))
    result, llm = _answer_run(ctx, s, "Revenue by region?", memory=store)
    assert result.state.prior_insights == []
    assert "<prior_insights>" not in llm.calls[0]["messages"][0]["content"]


def test_memory_only_saves_critic_approved_runs(ctx, settings, tmp_path) -> None:
    s = _memory_settings(settings, tmp_path)
    s.max_critic_rounds = 1
    llm = FakeLLM(
        agent=[resp(tool_use("final_report", markdown="South 999", key_numbers={"south": 999}))],
        critic=[critic_json("revise", issues=["wrong"], fix="x")],
    )
    result = loop(llm, ctx, s).run("Revenue by region?")
    assert result.status is RunStatus.COMPLETED_WITH_ISSUES
    assert not result.state.memory_saved
    assert InsightStore(s.memory_db_path).all_for(dataset_fingerprint(ctx)) == []


def test_memory_failure_never_fails_the_run(ctx, settings, tmp_path) -> None:
    s = _memory_settings(settings, tmp_path)

    class Broken(InsightStore):
        def __init__(self) -> None:
            pass

        def search(self, *a: Any, **k: Any) -> list[Any]:
            raise OSError("disk gone")

        def add(self, *a: Any, **k: Any) -> int:
            raise OSError("disk gone")

    result, _ = _answer_run(ctx, s, "Revenue by region?", memory=Broken())
    assert result.status is RunStatus.COMPLETED
    assert result.state.memory_error == "OSError: disk gone"
    assert not result.state.memory_saved


def test_memory_stores_structured_headline(ctx, settings, tmp_path) -> None:
    s = _memory_settings(settings, tmp_path)
    s.structured_report_enabled = True
    llm = FakeLLM(
        agent=[
            resp(tool_use("run_sql", query=SQL)),
            resp(tool_use("final_report", **structured())),
        ],
        critic=[critic_json("pass")],
    )
    result = loop(llm, ctx, s).run("Revenue by region?")
    assert result.state.memory_saved
    (ins,) = InsightStore(s.memory_db_path).all_for(dataset_fingerprint(ctx))
    assert ins.headline.startswith("South has the most revenue")
    assert ins.caveats == ["One south row has NULL revenue."]
    assert ins.key_numbers == {"south": 200, "north": 150}
