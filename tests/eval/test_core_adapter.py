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
