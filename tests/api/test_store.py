from __future__ import annotations

from analyst_agent.api.models import Job, JobStatus
from analyst_agent.api.store import JobStore


def _job(i: int, status: JobStatus = JobStatus.succeeded) -> Job:
    return Job(id=f"job_{i}", dataset_id="ds", question="q", status=status)


def test_memory_only_store_roundtrip():
    store = JobStore(db_path=None)
    store.create(_job(1, JobStatus.queued))
    store.append_event("job_1", "plan", {"plan": "x"})
    store.append_event("job_1", "final", {})
    assert store.update("job_1", status=JobStatus.running).status == JobStatus.running
    assert [e.seq for e in store.events("job_1")] == [1, 2]
    assert [e.type for e in store.events("job_1", after_seq=1)] == ["final"]
    assert store.get("missing") is None and store.events("missing") == []
    assert not store.persistent


def test_eviction_falls_back_to_sqlite(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db", memory_limit=2)
    store.create(_job(1, JobStatus.running))
    for i in range(2, 6):
        store.create(_job(i))
        store.append_event(f"job_{i}", "plan", {"i": i})
    # Running job is never evicted; the oldest finished ones are.
    assert "job_1" in store._mem
    assert "job_2" not in store._mem
    reloaded = store.get("job_2")
    assert reloaded is not None and reloaded.status == JobStatus.succeeded
    assert store.events("job_2")[0].data == {"i": 2}


def test_eviction_without_sqlite_drops_jobs():
    store = JobStore(db_path=None, memory_limit=1)
    store.create(_job(1))
    store.create(_job(2))
    assert store.get("job_1") is None
    assert store.get("job_2") is not None


def test_restart_marks_unfinished_jobs_failed(tmp_path):
    db = tmp_path / "jobs.db"
    first = JobStore(db_path=db)
    first.create(_job(1, JobStatus.running))
    first.create(_job(2, JobStatus.succeeded))
    first.close()

    second = JobStore(db_path=db)
    interrupted = second.get("job_1")
    assert interrupted.status == JobStatus.failed
    assert "restart" in interrupted.error
    assert second.get("job_2").status == JobStatus.succeeded
