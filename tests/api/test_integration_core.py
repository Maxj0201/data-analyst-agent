"""End-to-end: the real ``core.AgentLoop`` + tools + sandbox behind the API, LLM scripted."""

from __future__ import annotations

import httpx
import pytest

import tests.conftest as core_conftest
from analyst_agent.api.app import create_app

from .conftest import parse_sse, upload, wait_for_job

pytestmark = pytest.mark.asyncio


async def test_real_loop_through_api(settings, monkeypatch):
    import analyst_agent.core as core_pkg

    h = core_conftest
    monkeypatch.setenv("AGENT_ARTIFACTS_DIR", str(settings.artifacts_dir))
    fake = h.FakeLLM(
        agent=[
            h.resp(
                h.text("<plan>sum revenue</plan>"),
                h.tool_use("run_sql", query="SELECT SUM(revenue) AS t FROM s"),
            ),
            h.resp(
                h.tool_use(
                    "final_report",
                    markdown="# Total\n\nRevenue is **350**.",
                    key_numbers={"total": 350},
                )
            ),
        ],
        critic=[h.critic_json("pass")],
    )
    monkeypatch.setattr(core_pkg, "LLMClient", lambda settings: fake)

    app = create_app(settings)  # default runner -> core.AgentLoop
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        dataset_id = await upload(c)  # stored as s.csv -> table "s"
        resp = await c.post("/ask", json={"dataset_id": dataset_id, "question": "Total revenue?"})
        job_id = resp.json()["job_id"]
        job = await wait_for_job(c, job_id, timeout=20)
        events = parse_sse((await c.get(f"/jobs/{job_id}/events")).text)
        report = (await c.get(f"/jobs/{job_id}/report")).text
    app.state.jobs.shutdown()

    assert job["status"] == "succeeded", job
    names = [n for n, _ in events]
    assert names.count("llm_call") == 3  # agent x2 + critic: every LLM call is observed
    for step in ("plan", "tool_call", "tool_result", "critic", "final", "end"):
        assert step in names
    call = next(d for n, d in events if n == "tool_call")
    result = next(d for n, d in events if n == "tool_result")
    assert call["id"] == result["id"]
    assert result["ok"] is True, result
    assert job["cost"]["llm_calls"] == 3
    assert job["cost"]["input_tokens"] == 300
    assert job["progress"]["max_turns"] is not None
    assert "350" in report
    # prompt versions travel from the trace into the API result (and the final event)
    from analyst_agent.core.prompts import prompt_versions

    assert job["result"]["prompts"] == prompt_versions()
    final = next(d for n, d in events if n == "final")
    assert final["prompts"] == prompt_versions()
