"""GET /datasets/samples: bundled sample datasets that /ask accepts like uploads."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from analyst_agent.api.app import create_app
from analyst_agent.api.samples import SampleCatalog, sample_id
from analyst_agent.core.config import Settings

from .conftest import wait_for_job

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def samples_dir(tmp_path: Path) -> Path:
    root = tmp_path / "samples"
    (root / "saas").mkdir(parents=True)
    (root / "saas" / "subscriptions.csv").write_text("region,mrr\nEU,10\nNA,20\n\n")
    (root / "misc").mkdir()
    (root / "misc" / "other.csv").write_text("a,b\n1,2\n")
    (root / "misc" / "notes.txt").write_text("not a dataset")
    (root / "misc" / "broken.parquet").write_bytes(b"nope")
    (root / ".hidden").mkdir()
    (root / ".hidden" / "x.csv").write_text("a\n1\n")
    return root


async def _client(settings: Settings, fake_agent) -> tuple[httpx.AsyncClient, object]:
    app = create_app(settings, runner=fake_agent)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t"), app


@pytest.mark.asyncio
async def test_samples_disabled_by_default(client):
    resp = await client.get("/datasets/samples")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_samples_listed_and_askable(settings, fake_agent, samples_dir):
    settings.samples_enabled = True
    settings.samples_dir = samples_dir
    client, app = await _client(settings, fake_agent)
    async with client:
        samples = (await client.get("/datasets/samples")).json()
        # Curated first, readable data files only, hidden dirs skipped.
        assert [s["name"] for s in samples] == ["saas/subscriptions", "misc/other"]
        saas = samples[0]
        assert saas["dataset_id"] == sample_id("saas/subscriptions")
        assert saas["title"] == "SaaS subscriptions"
        assert saas["columns"] == ["region", "mrr"]
        assert saas["rows"] == 2
        assert saas["questions"] and all(q.endswith("?") for q in saas["questions"])
        assert samples[1]["questions"] == []

        info = (await client.get(f"/datasets/{saas['dataset_id']}")).json()
        assert info["filename"] == "subscriptions.csv"
        assert info["columns"] == ["region", "mrr"]

        resp = await client.post(
            "/ask", json={"dataset_id": saas["dataset_id"], "question": "top region?"}
        )
        assert resp.status_code == 202, resp.text
        job = await wait_for_job(client, resp.json()["job_id"])
        assert job["status"] == "succeeded"
        # The agent reads the sample in place; its stem stays the table name.
        assert fake_agent.calls[-1]["dataset_path"] == samples_dir / "saas" / "subscriptions.csv"
    app.state.jobs.shutdown()


@pytest.mark.asyncio
async def test_samples_missing_dir_is_empty(settings, fake_agent, tmp_path):
    settings.samples_enabled = True
    settings.samples_dir = tmp_path / "nope"
    client, app = await _client(settings, fake_agent)
    async with client:
        assert (await client.get("/datasets/samples")).json() == []
    app.state.jobs.shutdown()


def test_bundled_benchmark_data_has_curated_questions():
    samples = SampleCatalog(REPO / "benchmarks" / "data").list()
    names = {s.name for s in samples}
    assert {"saas/subscriptions", "ecommerce/orders", "hr/employees"} <= names
    assert all(s.questions for s in samples), "every bundled sample needs example questions"
    # Adversarial benchmark fixtures (#15: injection column/file names, PII) are not offered.
    assert not [n for n in names if n.startswith("finance/") or n == "support/ticket_messages"]
