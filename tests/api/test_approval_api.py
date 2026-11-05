"""Human-in-the-loop over HTTP: the real AgentLoop waits for POST /jobs/{id}/approve."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import httpx
import pytest

pytest.importorskip("fastapi", reason="needs the 'api' extra: uv sync --extra api")

import tests.conftest as h  # noqa: E402
from analyst_agent.api.agent import AgentOutcome  # noqa: E402
from analyst_agent.api.app import create_app  # noqa: E402

from .conftest import parse_sse, upload, wait_for_job  # noqa: E402

pytestmark = pytest.mark.asyncio

Q1 = "SELECT SUM(revenue) AS t FROM s"
Q2 = "SELECT * FROM s"


@pytest.fixture
def gated_core(settings, monkeypatch):
    """Real core runner with SQL row gating on (the 2-row table exceeds threshold 0)."""
    import analyst_agent.core as core_pkg

    monkeypatch.setenv("AGENT_ARTIFACTS_DIR", str(settings.artifacts_dir))
    monkeypatch.setenv("AGENT_APPROVAL_ENABLED", "true")
    monkeypatch.setenv("AGENT_APPROVAL_ROW_THRESHOLD", "0")
    monkeypatch.setenv("AGENT_APPROVAL_PYTHON", "false")
    monkeypatch.delenv("AGENT_APPROVAL_BUDGET_FRACTION", raising=False)  # 0.8 of 400k: unreached
    monkeypatch.setenv("AGENT_APPROVAL_TIMEOUT_S", "10")
    fake = h.FakeLLM(
        agent=[
            h.resp(h.text("<plan>sum</plan>"), h.tool_use("run_sql", query=Q1)),
            h.resp(h.tool_use("run_sql", query=Q2)),
            h.resp(h.tool_use("final_report", markdown="# Total\n\n**350**", key_numbers={})),
        ],
        critic=[h.critic_json("pass")],
    )
    monkeypatch.setattr(core_pkg, "LLMClient", lambda settings: fake)
    return fake


async def wait_for_pending(client: httpx.AsyncClient, job_id: str, after: str | None = None):
    deadline = asyncio.get_running_loop().time() + 10
    while True:
        body = (await client.get(f"/jobs/{job_id}")).json()
        pending = body.get("pending_approval")
        if pending and pending["id"] != after:
            return pending
        assert body["status"] not in {"succeeded", "partial", "failed"}, body
        assert asyncio.get_running_loop().time() < deadline, "no approval became pending"
        await asyncio.sleep(0.02)


async def test_approve_and_deny_round_trip(settings, gated_core):
    app = create_app(settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        dataset_id = await upload(c)
        job_id = (await c.post("/ask", json={"dataset_id": dataset_id, "question": "?"})).json()[
            "job_id"
        ]

        first = await wait_for_pending(c, job_id)
        assert first["tool"] == "run_sql" and first["input"] == {"query": Q1}
        assert first["triggers"] == ["sql_rows"] and first["estimated_rows"] >= 1
        assert (await c.get(f"/jobs/{job_id}")).json()["progress"]["phase"] == "approval_required"

        stale = await c.post(
            f"/jobs/{job_id}/approve", json={"approve": True, "action_id": "appr_stale"}
        )
        assert stale.status_code == 409
        ok = await c.post(f"/jobs/{job_id}/approve", json={"approve": True})
        assert ok.status_code == 200
        assert ok.json() == {"job_id": job_id, "action_id": first["id"], "approved": True}

        second = await wait_for_pending(c, job_id, after=first["id"])
        assert second["input"] == {"query": Q2}
        denied = await c.post(
            f"/jobs/{job_id}/approve",
            json={"approve": False, "note": "too broad", "action_id": second["id"]},
        )
        assert denied.status_code == 200 and denied.json()["approved"] is False

        job = await wait_for_job(c, job_id, timeout=20)
        events = parse_sse((await c.get(f"/jobs/{job_id}/events")).text)
        late = await c.post(f"/jobs/{job_id}/approve", json={"approve": True})
    app.state.jobs.shutdown()

    assert job["status"] == "succeeded", job
    assert job["pending_approval"] is None
    assert late.status_code == 409  # job finished

    names = [n for n, _ in events]
    assert names.count("approval_required") == 2
    decisions = [d for n, d in events if n == "approval_decision"]
    assert [(d["approved"], d["decided_by"], d["note"]) for d in decisions] == [
        (True, "human", None),
        (False, "human", "too broad"),
    ]
    assert decisions[0]["action_id"] == first["id"]
    results = [d for n, d in events if n == "tool_result" and d["name"] == "run_sql"]
    assert [r["ok"] for r in results] == [True, False]
    assert "too broad" in results[1]["summary"]
    # The trace (GET /jobs/{id}) holds the same approval events.
    assert sum(e["type"] == "approval_decision" for e in job["trace"]) == 2
    # The model got the denial as a tool error and could re-plan.
    last_user = gated_core.calls_for("agent")[2]["messages"][-1]["content"]
    (block,) = [b for b in last_user if b.get("type") == "tool_result"]
    assert block["is_error"] is True and "too broad" in block["content"]


async def test_no_decision_times_out_as_denied(settings, gated_core, monkeypatch):
    monkeypatch.setenv("AGENT_APPROVAL_TIMEOUT_S", "0.2")
    app = create_app(settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        dataset_id = await upload(c)
        job_id = (await c.post("/ask", json={"dataset_id": dataset_id, "question": "?"})).json()[
            "job_id"
        ]
        job = await wait_for_job(c, job_id, timeout=20)
        events = parse_sse((await c.get(f"/jobs/{job_id}/events")).text)
    app.state.jobs.shutdown()

    assert job["status"] == "succeeded"
    decisions = [d for n, d in events if n == "approval_decision"]
    assert [d["decided_by"] for d in decisions] == ["timeout", "timeout"]
    assert all(not d["approved"] for d in decisions)
    results = [d for n, d in events if n == "tool_result" and d["name"] == "run_sql"]
    assert all(not r["ok"] and "no decision within 0.2s" in r["summary"] for r in results)


async def test_approve_unknown_job_is_404(client):
    resp = await client.post("/jobs/job_nope/approve", json={"approve": True})
    assert resp.status_code == 404


async def test_approve_without_pending_is_409(client, fake_agent):
    gate = fake_agent.gate = threading.Event()
    dataset_id = await upload(client)
    job_id = (await client.post("/ask", json={"dataset_id": dataset_id, "question": "?"})).json()[
        "job_id"
    ]
    resp = await client.post(f"/jobs/{job_id}/approve", json={"approve": True})
    gate.set()
    await wait_for_job(client, job_id)
    assert resp.status_code == 409


async def test_approve_validates_body(client):
    dataset_id = await upload(client)
    job_id = (await client.post("/ask", json={"dataset_id": dataset_id, "question": "?"})).json()[
        "job_id"
    ]
    assert (await client.post(f"/jobs/{job_id}/approve", json={})).status_code == 422
    assert (
        await client.post(f"/jobs/{job_id}/approve", json={"approve": True, "note": "x" * 3000})
    ).status_code == 422


async def test_runner_receives_approver_only_if_it_accepts_one(settings):
    seen: list[object] = []

    def runner(*, dataset_path: Path, question: str, emit, approver=None) -> AgentOutcome:
        seen.append(approver)
        return AgentOutcome(report_markdown="ok")

    app = create_app(settings, runner=runner)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        dataset_id = await upload(c)
        job_id = (await c.post("/ask", json={"dataset_id": dataset_id, "question": "?"})).json()[
            "job_id"
        ]
        await wait_for_job(c, job_id)
    app.state.jobs.shutdown()
    assert len(seen) == 1 and hasattr(seen[0], "request")
