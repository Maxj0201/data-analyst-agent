from __future__ import annotations

import asyncio
import threading

import httpx
import pytest

from analyst_agent.api.agent import AgentOutcome
from analyst_agent.api.app import create_app

from .conftest import CSV_BYTES, PNG_BYTES, parse_sse, upload, wait_for_job

pytestmark = pytest.mark.asyncio


# ---- datasets ------------------------------------------------------------------------------


async def test_upload_csv_stores_file_under_uploads(client, settings):
    resp = await client.post("/datasets", files={"file": ("sales.csv", CSV_BYTES, "text/csv")})
    assert resp.status_code == 201
    body = resp.json()
    assert body["dataset_id"].startswith("ds_")
    assert body["columns"] == ["region", "revenue"]
    assert body["filename"] == "sales.csv"
    assert body["stored_name"] == "sales.csv"
    stored = settings.uploads_dir / body["dataset_id"] / "sales.csv"
    assert stored.read_bytes() == CSV_BYTES

    info = await client.get(f"/datasets/{body['dataset_id']}")
    assert info.status_code == 200 and info.json()["size_bytes"] == len(CSV_BYTES)


@pytest.mark.parametrize(
    ("name", "content", "status"),
    [
        ("notes.txt", b"a,b\n1,2\n", 415),
        ("empty.csv", b"", 400),
        ("bin.csv", b"\x00\x01\x02binary", 400),
        ("latin.csv", "région,x\n".encode("latin-1"), 400),
    ],
)
async def test_upload_rejects_bad_files(client, settings, name, content, status):
    resp = await client.post("/datasets", files={"file": (name, content, "text/csv")})
    assert resp.status_code == status, resp.text
    assert list(settings.uploads_dir.iterdir()) == []


async def test_upload_enforces_size_limit(client, settings):
    big = b"a,b\n" + b"1,2\n" * (300 * 1024)  # > 1 MB limit configured in the fixture
    resp = await client.post("/datasets", files={"file": ("big.csv", big, "text/csv")})
    assert resp.status_code == 413
    assert list(settings.uploads_dir.iterdir()) == []


async def test_upload_filename_is_sanitised(client, settings):
    resp = await client.post(
        "/datasets", files={"file": ("../../Q3 sales (final).csv", CSV_BYTES, "text/csv")}
    )
    body = resp.json()
    assert body["filename"] == "Q3 sales (final).csv"
    assert body["stored_name"] == "Q3_sales_final.csv"
    assert (settings.uploads_dir / body["dataset_id"] / "Q3_sales_final.csv").is_file()


async def test_unknown_dataset_is_404(client):
    assert (await client.get("/datasets/ds_0000000000000000")).status_code == 404
    assert (await client.get("/datasets/../../etc/passwd")).status_code == 404
    resp = await client.post("/ask", json={"dataset_id": "ds_nope", "question": "hi"})
    assert resp.status_code == 404


# ---- ask / jobs ----------------------------------------------------------------------------


async def test_ask_runs_job_and_exposes_result(client, fake_agent, settings):
    dataset_id = await upload(client)
    resp = await client.post("/ask", json={"dataset_id": dataset_id, "question": "Total revenue?"})
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    assert resp.json()["events_url"] == f"/jobs/{job_id}/events"

    job = await wait_for_job(client, job_id)
    assert job["status"] == "succeeded"
    assert job["question"] == "Total revenue?"
    assert job["progress"]["fraction"] == 1.0
    assert job["progress"]["max_turns"] == 4
    assert job["duration_seconds"] >= 0
    # Cost is summed from llm_call events.
    assert job["cost"]["input_tokens"] == 150
    assert job["cost"]["output_tokens"] == 50
    assert job["cost"]["llm_calls"] == 2
    assert job["cost"]["usd"] == pytest.approx(0.03)

    types_ = [e["type"] for e in job["trace"]]
    assert types_ == [
        "status", "status", "plan", "llm_call", "tool_call", "tool_result", "llm_call",
        "critic", "final",
    ]  # fmt: skip
    assert [e["seq"] for e in job["trace"]] == list(range(1, len(types_) + 1))

    result = job["result"]
    assert result["key_numbers"] == {"total_revenue": 350}
    assert result["artifacts"] == [{"path": "revenue.png", "url": "/artifacts/revenue.png"}]
    assert fake_agent.calls[0]["dataset_path"] == settings.uploads_dir / dataset_id / "s.csv"


async def test_report_is_markdown_with_artifact_links_rewritten(client):
    dataset_id = await upload(client)
    job_id = (await client.post("/ask", json={"dataset_id": dataset_id, "question": "q"})).json()[
        "job_id"
    ]
    await wait_for_job(client, job_id)
    resp = await client.get(f"/jobs/{job_id}/report")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "Total is **350**" in resp.text
    assert "![chart](/artifacts/revenue.png)" in resp.text


async def test_report_conflict_while_running(client, fake_agent):
    fake_agent.gate = threading.Event()
    dataset_id = await upload(client)
    job_id = (await client.post("/ask", json={"dataset_id": dataset_id, "question": "q"})).json()[
        "job_id"
    ]
    resp = await client.get(f"/jobs/{job_id}/report")
    assert resp.status_code == 409
    fake_agent.gate.set()
    await wait_for_job(client, job_id)


async def test_unknown_job_is_404(client):
    for url in ("/jobs/job_x", "/jobs/job_x/report", "/jobs/job_x/events"):
        assert (await client.get(url)).status_code == 404


async def test_agent_exception_marks_job_failed(client, fake_agent):
    def boom(**_):
        raise ValueError("sandbox exploded")

    fake_agent.behaviour = boom
    dataset_id = await upload(client)
    job_id = (await client.post("/ask", json={"dataset_id": dataset_id, "question": "q"})).json()[
        "job_id"
    ]
    job = await wait_for_job(client, job_id)
    assert job["status"] == "failed"
    assert "sandbox exploded" in job["error"]
    assert [e["type"] for e in job["trace"]][-2:] == ["error", "final"]
    assert (await client.get(f"/jobs/{job_id}/report")).status_code == 404


async def test_partial_outcome_uses_reported_cost(client, fake_agent):
    def partial(*, emit, **_):
        emit("llm_call", {"input_tokens": 10, "output_tokens": 1, "usd": 0.001})
        return AgentOutcome(
            status="partial",
            report_markdown="Stopped: token budget exceeded.",
            cost={"input_tokens": 999, "output_tokens": 9, "total_usd": 1.5},
        )

    fake_agent.behaviour = partial
    dataset_id = await upload(client)
    job_id = (await client.post("/ask", json={"dataset_id": dataset_id, "question": "q"})).json()[
        "job_id"
    ]
    job = await wait_for_job(client, job_id)
    assert job["status"] == "partial"
    assert job["cost"]["input_tokens"] == 999
    assert job["cost"]["usd"] == 1.5
    assert job["cost"]["llm_calls"] == 1


@pytest.mark.parametrize(
    "body",
    [{"dataset_id": "x"}, {"question": "q"}, {"dataset_id": "x", "question": ""}],
)
async def test_ask_validates_body(client, body):
    assert (await client.post("/ask", json=body)).status_code == 422


# ---- SSE -----------------------------------------------------------------------------------


async def test_sse_streams_steps_live_then_ends(client, fake_agent):
    fake_agent.gate = threading.Event()
    dataset_id = await upload(client)
    job_id = (await client.post("/ask", json={"dataset_id": dataset_id, "question": "q"})).json()[
        "job_id"
    ]
    # Connect while the agent is still blocked mid-run, then let it finish.
    stream_task = asyncio.create_task(client.get(f"/jobs/{job_id}/events"))
    await asyncio.sleep(0.1)
    assert not stream_task.done()
    fake_agent.gate.set()
    resp = await asyncio.wait_for(stream_task, timeout=5)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(resp.text)
    names = [n for n, _ in events]
    for step in ("plan", "tool_call", "tool_result", "critic", "final"):
        assert step in names
    assert names[-1] == "end"
    assert events[-1][1] == {"status": "succeeded"}
    tool_result = dict(events)["tool_result"]
    assert tool_result["summary"] == "1 row" and tool_result["name"] == "run_sql"
    assert dict(events)["critic"]["verdict"] == "pass"
    assert "id: 1\n" in resp.text


async def test_sse_resumes_from_last_event_id(client):
    dataset_id = await upload(client)
    job_id = (await client.post("/ask", json={"dataset_id": dataset_id, "question": "q"})).json()[
        "job_id"
    ]
    job = await wait_for_job(client, job_id)
    total = len(job["trace"])
    resp = await client.get(f"/jobs/{job_id}/events", headers={"Last-Event-ID": str(total - 2)})
    seqs = [d["seq"] for n, d in parse_sse(resp.text) if n != "end"]
    assert seqs == [total - 1, total]


# ---- artifacts -----------------------------------------------------------------------------


async def test_artifacts_served_and_traversal_blocked(client, settings, tmp_path):
    (settings.artifacts_dir / "sub").mkdir(parents=True)
    (settings.artifacts_dir / "sub" / "c.png").write_bytes(PNG_BYTES)
    (settings.artifacts_dir / "notes.txt").write_text("secret")
    (tmp_path / "outside.png").write_bytes(PNG_BYTES)

    ok = await client.get("/artifacts/sub/c.png")
    assert ok.status_code == 200
    assert ok.headers["content-type"] == "image/png"
    assert ok.content == PNG_BYTES

    for bad in ("notes.txt", "../outside.png", "..%2Foutside.png", "missing.png"):
        assert (await client.get(f"/artifacts/{bad}")).status_code == 404, bad


async def test_artifact_outside_dir_is_copied_in(client, fake_agent, tmp_path):
    elsewhere = tmp_path / "scratch" / "plot.png"
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(PNG_BYTES)

    def run(**_):
        return AgentOutcome(report_markdown=f"![p]({elsewhere})", artifacts=[str(elsewhere)])

    fake_agent.behaviour = run
    dataset_id = await upload(client)
    job_id = (await client.post("/ask", json={"dataset_id": dataset_id, "question": "q"})).json()[
        "job_id"
    ]
    job = await wait_for_job(client, job_id)
    url = job["result"]["artifacts"][0]["url"]
    assert url == f"/artifacts/{job_id}/plot.png"
    assert (await client.get(url)).content == PNG_BYTES
    assert f"![p]({url})" in (await client.get(f"/jobs/{job_id}/report")).text


# ---- persistence ---------------------------------------------------------------------------


async def test_finished_jobs_survive_restart_via_sqlite(settings, fake_agent):
    app = create_app(settings, runner=fake_agent)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        dataset_id = await upload(c)
        job_id = (await c.post("/ask", json={"dataset_id": dataset_id, "question": "q"})).json()[
            "job_id"
        ]
        await wait_for_job(c, job_id)
    app.state.jobs.shutdown()

    app2 = create_app(settings, runner=fake_agent)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="http://t") as c:
        job = (await c.get(f"/jobs/{job_id}")).json()
        assert job["status"] == "succeeded"
        assert len(job["trace"]) == 9
        assert "350" in (await c.get(f"/jobs/{job_id}/report")).text
    app2.state.jobs.shutdown()


async def test_healthz(client):
    body = (await client.get("/healthz")).json()
    assert body == {"status": "ok", "job_store": "memory+sqlite", "langfuse": False}
