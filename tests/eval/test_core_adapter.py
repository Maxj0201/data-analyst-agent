"""eval's ``core`` agent adapter drives the real AgentLoop + tools + sandbox (LLM scripted)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import analyst_agent.core as core_pkg
from analyst_agent.eval.agent import AgentInput, core_agent
from tests.conftest import FakeLLM, critic_json, resp, text, tool_use


def test_core_agent_end_to_end(sales_csv: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENT_ARTIFACTS_DIR", str(tmp_path / "art"))
    fake = FakeLLM(
        agent=[
            resp(
                text("<plan>sum revenue</plan>"),
                tool_use("run_sql", query="SELECT SUM(revenue) AS t FROM sales"),
            ),
            resp(tool_use("final_report", markdown="Revenue is 350.", key_numbers={"t": 350})),
        ],
        critic=[critic_json("pass")],
    )
    monkeypatch.setattr(core_pkg, "LLMClient", lambda settings: fake)

    out = asyncio.run(core_agent(AgentInput("t1", "Total revenue?", [sales_csv])))

    assert out.status == "completed", out.error
    assert out.key_numbers == {"t": 350}
    calls = [e for e in out.trace if e["type"] == "tool_call"]
    assert calls[0]["name"] == "run_sql" and not calls[0]["is_error"]
    assert "350" in calls[0]["output"]


def test_core_agent_routing_trace_and_ab_override(
    sales_csv: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Routing forced on via load_agent (eval A/B): route + escalation reach the trace."""
    from analyst_agent.eval.agent import load_agent
    from analyst_agent.eval.runner import TaskResult

    monkeypatch.setenv("AGENT_ARTIFACTS_DIR", str(tmp_path / "art"))
    monkeypatch.setenv("AGENT_ROUTING_ENABLED", "false")  # the override must win
    monkeypatch.setenv("AGENT_MODEL_L1", "claude-sonnet-5-5")
    bad = "SELECT nope FROM sales"
    fake = FakeLLM(
        router=[resp(text('{"tier": "L1", "reason": "one sum"}'), model="claude-haiku-5-5")],
        agent=[
            resp(tool_use("run_sql", query=bad), model="claude-sonnet-5-5"),
            resp(tool_use("run_sql", query=bad), model="claude-sonnet-5-5"),
            resp(tool_use("final_report", markdown="Revenue is 350.", key_numbers={"t": 350})),
        ],
        critic=[critic_json("pass")],
    )
    monkeypatch.setattr(core_pkg, "LLMClient", lambda settings: fake)

    agent = load_agent("core", routing_enabled=True)
    out = asyncio.run(agent(AgentInput("t1", "Total revenue?", [sales_csv])))

    assert out.status == "completed", out.error
    assert out.trace[0]["type"] == "route" and out.trace[0]["tier"] == "L1"
    [esc] = [e for e in out.trace if e["type"] == "escalation"]
    assert (esc["from_tier"], esc["to_tier"], esc["trigger"]) == ("L1", "L2", "tool_error")
    calls = [e for e in out.trace if e["type"] == "tool_call"]
    # L2 is unset, so it falls back to AGENT_MODEL.
    assert [c["model"] for c in calls] == ["claude-sonnet-5-5"] * 2 + ["claude-opus-5-5"]
    assert out.routing["routed_tier"] == "L1" and out.routing["final_tier"] == "L2"
    assert set(out.cost_by_model) == {"claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"}
    assert sum(m["usd"] for m in out.cost_by_model.values()) == pytest.approx(out.cost_usd)
    assert "routing" in TaskResult.__dataclass_fields__

    # Routing forced off: no router call is made (the router queue would otherwise be consumed).
    fake2 = FakeLLM(
        agent=[resp(tool_use("final_report", markdown="x", key_numbers={}))],
        critic=[critic_json("pass")],
    )
    monkeypatch.setenv("AGENT_ROUTING_ENABLED", "true")
    monkeypatch.setattr(core_pkg, "LLMClient", lambda settings: fake2)
    out2 = asyncio.run(
        load_agent("core", routing_enabled=False)(AgentInput("t2", "q", [sales_csv]))
    )
    assert out2.routing is None and not any(e["type"] == "route" for e in out2.trace)
