from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

from analyst_agent.api.agent import AgentOutcome, EmitFn
from analyst_agent.core.config import Settings

# Test modules that need FastAPI start with ``pytest.importorskip("fastapi", ...)``: without
# the ``api`` extra (a plain ``uv sync``) they are reported as skipped instead of erroring.

# Smallest valid PNG (1x1 transparent pixel).
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63f8cfc0f01f0005000201a5b9d5d50000000049454e44ae426082"
)
CSV_BYTES = b"region,revenue\nnorth,100\nsouth,250\n"


class FakeAgent:
    """Stands in for the agent loop: emits a realistic step sequence and writes a chart."""

    def __init__(self, artifacts_dir: Path) -> None:
        self.artifacts_dir = artifacts_dir
        self.gate: threading.Event | None = None
        self.calls: list[dict[str, Any]] = []
        self.behaviour: Callable[..., AgentOutcome] | None = None

    def __call__(self, *, dataset_path: Path, question: str, emit: EmitFn) -> AgentOutcome:
        self.calls.append({"dataset_path": dataset_path, "question": question})
        if self.behaviour is not None:
            return self.behaviour(dataset_path=dataset_path, question=question, emit=emit)
        emit("plan", {"plan": "1. inspect schema 2. sum revenue", "turn": 1, "max_turns": 4})
        if self.gate is not None:
            assert self.gate.wait(timeout=5), "test never released the agent"
        emit("llm_call", {"model": "m", "input_tokens": 100, "output_tokens": 20, "usd": 0.01})
        emit("tool_call", {"id": "t1", "name": "run_sql", "input": {"query": "SELECT 1"}})
        emit("tool_result", {"id": "t1", "name": "run_sql", "ok": True, "summary": "1 row"})
        emit("llm_call", {"model": "m", "input_tokens": 50, "output_tokens": 30, "usd": 0.02})
        emit("critic", {"verdict": "pass", "issues": [], "suggested_fix": "", "turn": 2})
        chart = self.artifacts_dir / "revenue.png"
        chart.write_bytes(PNG_BYTES)
        return AgentOutcome(
            report_markdown="# Revenue\n\nTotal is **350**.\n\n![chart](artifacts/revenue.png)\n",
            key_numbers={"total_revenue": 350},
            artifacts=["artifacts/revenue.png"],
        )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path / "artifacts",
        job_db_path=tmp_path / "data" / "jobs.sqlite3",
        max_upload_mb=1,
        langfuse_enabled=False,
    )


@pytest.fixture
def fake_agent(settings: Settings) -> FakeAgent:
    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
    return FakeAgent(settings.artifacts_dir)


@pytest_asyncio.fixture
async def client(settings: Settings, fake_agent: FakeAgent) -> AsyncIterator[httpx.AsyncClient]:
    from analyst_agent.api.app import create_app  # needs the 'api' extra

    app = create_app(settings, runner=fake_agent)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.state.jobs.shutdown()


async def upload(client: httpx.AsyncClient, content: bytes = CSV_BYTES, name: str = "s.csv") -> str:
    resp = await client.post("/datasets", files={"file": (name, content, "text/csv")})
    assert resp.status_code == 201, resp.text
    return resp.json()["dataset_id"]


async def wait_for_job(client: httpx.AsyncClient, job_id: str, timeout: float = 5.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        body = (await client.get(f"/jobs/{job_id}")).json()
        if body["status"] in {"succeeded", "partial", "failed"}:
            return body
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"job did not finish: {body}")
        await asyncio.sleep(0.02)


def parse_sse(text: str) -> list[tuple[str, dict]]:
    import json

    events = []
    for block in text.split("\n\n"):
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if name is not None:
            events.append((name, data))
    return events
