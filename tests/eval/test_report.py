from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from analyst_agent.eval.cli import app
from analyst_agent.eval.report import percentile, render_compare, render_report, summarize


def r(task_id: str, difficulty: str, passed: bool, **kw: Any) -> dict[str, Any]:
    base = {
        "task_id": task_id,
        "difficulty": difficulty,
        "passed": passed,
        "score": 1.0 if passed else 0.0,
        "status": "ok",
        "latency_s": 1.0,
        "cost_usd": 0.1,
        "tool_calls": 2,
        "critic_rounds": 1,
        "critic_revisions": 0,
    }
    return {**base, **kw}


RUN_A = [
    r("a", "L1", True, latency_s=1.0),
    r("b", "L1", False, latency_s=2.0, status="timeout"),
    r("c", "L2", True, latency_s=3.0, critic_revisions=1),
    r("d", "ADV", False, latency_s=4.0, critic_revisions=1),
    r("e", "ADV", True, latency_s=10.0, cost_usd=0.5),
]


def test_percentile() -> None:
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([5], 95) == 5
    assert math.isnan(percentile([], 50))


def test_summarize_metrics() -> None:
    s = summarize(RUN_A)
    assert s["n"] == 5 and s["accuracy"] == 0.6
    assert s["by_difficulty"]["L1"]["accuracy"] == 0.5
    assert s["by_difficulty"]["L1"]["timeouts"] == 1
    assert s["adversarial_pass_rate"] == 0.5
    assert math.isclose(s["mean_cost_usd"], 0.18)
    assert s["latency_p50_s"] == 3.0
    assert math.isclose(s["latency_p95_s"], 8.8)
    assert s["critic_intervention_rate"] == 0.4
    assert s["critic_correction_rate"] == 0.5
    assert "L3" not in s["by_difficulty"]


def test_text_fallback_rate() -> None:
    rows = [
        r("a", "L1", True, score_details={"method": "numeric", "path": "key_numbers"}),
        r("b", "L1", True, score_details={"method": "numeric", "path": "text_fallback"}),
        r("c", "L2", True, score_details={"method": "rows"}),
        r(
            "d",
            "ADV",
            True,
            score_details={
                "method": "adversarial",
                "answer": {"method": "numeric", "details": {"path": "text_fallback"}},
            },
        ),
    ]
    s = summarize(rows)
    assert s["numeric_tasks"] == 3
    assert math.isclose(s["text_fallback_rate"], 2 / 3)
    assert "| Numeric text-fallback rate | 66.7% |" in render_report(rows)
    assert summarize(RUN_A)["text_fallback_rate"] is None


def test_render_report_markdown() -> None:
    md = render_report(RUN_A, {"config": {"agent": "x"}, "git_sha": "abc"})
    assert "| Accuracy (all) | 60.0% |" in md
    assert "| Adversarial pass rate | 50.0% |" in md
    assert "| L1 | 2 | 1 | 50.0% |" in md
    assert "| Critic correction rate | 50.0% |" in md


def test_render_compare_flips() -> None:
    run_b = [
        r("a", "L1", False),
        r("b", "L1", True),
        r("c", "L2", True),
        r("d", "ADV", True),
        r("f", "L3", True),
    ]
    md = render_compare(RUN_A, run_b, "base", "new")
    assert "Newly passing in new (2): `b`, `d`" in md
    assert "Regressions in new (1): `a`" in md
    assert "Only in base: 1; only in new: 1" in md
    assert "| Accuracy (all) | 60.0% | 80.0% | +20.0 pp |" in md


def test_cli_run_report_compare(tmp_path: Path) -> None:
    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "run",
            "--agent",
            "oracle",
            "--no-judge",
            "-d",
            "L1",
            "-d",
            "L2",
            "--limit",
            "4",
            "--runs-dir",
            str(tmp_path),
        ],
    )
    assert res.exit_code == 0, res.output
    run_dirs = sorted(p for p in tmp_path.iterdir() if p.is_dir())
    assert len(run_dirs) == 1
    d = run_dirs[0]
    rows = [json.loads(x) for x in (d / "results.jsonl").read_text().splitlines()]
    assert len(rows) == 4 and all(x["passed"] for x in rows)
    assert (d / "report.md").exists()

    res = runner.invoke(app, ["report", str(d)])
    assert res.exit_code == 0 and "100.0%" in res.output
    res = runner.invoke(app, ["compare", str(d), str(d), "--out", str(tmp_path / "c.md")])
    assert res.exit_code == 0 and (tmp_path / "c.md").exists()
    res = runner.invoke(app, ["list", "-d", "ADV"])
    assert res.exit_code == 0 and res.output.count("adv_") == 10


# --------------------------------------------------------------------------- routing section


def routing(tier: str, *triggers: str, source: str = "router") -> dict[str, Any]:
    tiers = ["L1", "L2", "L3"]
    escalations, cur = [], tier
    for t in triggers:
        nxt = tiers[tiers.index(cur) + 1]
        escalations.append({"from_tier": cur, "to_tier": nxt, "trigger": t})
        cur = nxt
    return {
        "enabled": True,
        "routed_tier": tier,
        "final_tier": cur,
        "route": {"tier": tier, "source": source},
        "escalations": escalations,
    }


def cbm(model: str, usd: float, calls: int = 1) -> dict[str, dict[str, Any]]:
    return {
        model: {
            "calls": calls,
            "input_tokens": 100,
            "cache_read_input_tokens": 50,
            "output_tokens": 10,
            "usd": usd,
        }
    }


ROUTED = [
    r("a", "L1", True, cost_usd=0.01, routing=routing("L1"), cost_by_model=cbm("haiku", 0.01)),
    r("b", "L1", False, cost_usd=0.03, routing=routing("L1", "tool_error")),
    r("c", "L2", True, cost_usd=0.10, routing=routing("L2", "critic_revise")),
    r(
        "d",
        "L2",
        True,
        cost_usd=0.20,
        routing=routing("L1", "tool_error", "critic_revise"),
        cost_by_model=cbm("opus", 0.20, calls=3),
    ),
    r("e", "L3", False, cost_usd=0.50, routing=routing("L3", source="fallback")),
    r("f", "L3", False, cost_usd=0.0, status="timeout"),  # no routing info
]


def test_summarize_routing() -> None:
    from analyst_agent.eval.report import summarize_routing

    assert summarize_routing(RUN_A) is None  # routing off -> no section
    rt = summarize_routing(ROUTED)
    assert rt is not None
    assert rt["routed"] == 5 and rt["unrouted"] == 1 and rt["router_fallbacks"] == 1
    assert rt["escalation_rate"] == 0.6

    l1 = rt["by_tier"]["L1"]
    assert (l1["n"], l1["passed"], l1["escalated"]) == (3, 2, 2)
    assert math.isclose(l1["accuracy"], 2 / 3)
    assert math.isclose(l1["mean_cost_usd"], 0.08)
    assert math.isclose(l1["total_cost_usd"], 0.24)
    assert rt["by_tier"]["L2"]["n"] == 1 and rt["by_tier"]["L3"]["accuracy"] == 0.0

    esc = rt["by_escalation"]
    assert esc["not escalated"]["n"] == 2 and esc["not escalated"]["accuracy"] == 0.5
    assert esc["escalated"]["n"] == 3
    assert math.isclose(esc["escalated"]["accuracy"], 2 / 3)
    assert math.isclose(esc["escalated"]["mean_cost_usd"], 0.33 / 3)
    assert esc["trigger: tool_error"]["n"] == 2  # b and d (d also on critic_revise)
    assert esc["trigger: critic_revise"]["n"] == 2  # c and d
    assert rt["escalation_paths"] == {"L1→L2": 1, "L1→L2→L3": 1, "L2→L3": 1}

    assert rt["vs_difficulty"] == {"L1": {"L1": 2}, "L2": {"L1": 1, "L2": 1}, "L3": {"L3": 1}}
    assert rt["by_model"]["opus"] == {
        "calls": 3,
        "input_tokens": 150,
        "output_tokens": 10,
        "usd": 0.20,
    }
    assert list(rt["by_model"]) == ["opus", "haiku"]  # most expensive first


def test_render_report_routing_section() -> None:
    md = render_report(ROUTED)
    assert "## Routing" in md
    assert "| L1 | 3 | 2 | 66.7% | $0.0800 | $0.2400 | 2 |" in md
    assert "| escalated | 3 | 2 | 66.7% | $0.1100 | $0.3300 | 3 |" in md
    assert "| trigger: tool_error | 2 |" in md
    assert "| L2 | 1 | 1 | 0 |" in md  # routed tier vs labelled difficulty
    assert "| opus | 3 | 150 | 10 | $0.2000 |" in md
    assert "router fallbacks: 1" in md
    assert "## Routing" not in render_report(RUN_A)


def test_render_compare_routing_rows() -> None:
    md = render_compare(RUN_A, ROUTED, "base", "routed")
    assert "| Routing | off | on | |" in md
    assert "| Escalation rate | – | 60.0% | – |" in md
    assert "| Accuracy routed L1 | – | 66.7% | – |" in md
    assert "Escalation rate" not in render_compare(RUN_A, RUN_A)
