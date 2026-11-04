"""Job store: in-memory primary with a write-through SQLite fallback.

Reads hit memory first. Every write is mirrored to SQLite (when configured), so jobs that were
evicted from memory, or that belong to a previous process, can still be served. Live event
streaming only ever needs the in-memory copy of a running job.

All methods are synchronous and thread-safe: the agent runs in worker threads and calls
``append_event`` from there, while the SSE endpoint polls ``events`` from the event loop.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import Job, JobEvent, JobStatus


@dataclass
class _Entry:
    job: Job
    events: list[JobEvent] = field(default_factory=list)


class _SqliteBackend:
    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        body TEXT NOT NULL,
        created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS job_events (
        job_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        type TEXT NOT NULL,
        ts REAL NOT NULL,
        data TEXT NOT NULL,
        PRIMARY KEY (job_id, seq)
    );
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(self._SCHEMA)
        self._lock = threading.Lock()

    def save_job(self, job: Job) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id, body, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET body = excluded.body",
                (job.id, job.model_dump_json(), job.created_at),
            )

    def save_event(self, job_id: str, event: JobEvent) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO job_events (job_id, seq, type, ts, data) "
                "VALUES (?, ?, ?, ?, ?)",
                (job_id, event.seq, event.type, event.ts, json.dumps(event.data, default=str)),
            )

    def load(self, job_id: str) -> _Entry | None:
        with self._lock:
            row = self._conn.execute("SELECT body FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return None
            rows = self._conn.execute(
                "SELECT seq, type, ts, data FROM job_events WHERE job_id = ? ORDER BY seq",
                (job_id,),
            ).fetchall()
        events = [JobEvent(seq=s, type=t, ts=ts, data=json.loads(d)) for s, t, ts, d in rows]
        return _Entry(job=Job.model_validate_json(row[0]), events=events)

    def mark_interrupted(self) -> int:
        """Fail jobs left non-terminal by a previous process (they can never finish)."""
        with self._lock:
            rows = self._conn.execute("SELECT id, body FROM jobs").fetchall()
        n = 0
        for _, body in rows:
            job = Job.model_validate_json(body)
            if not job.status.is_terminal:
                job.status = JobStatus.failed
                job.error = "Job was interrupted by a server restart."
                job.finished_at = time.time()
                self.save_job(job)
                n += 1
        return n

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class JobStore:
    def __init__(self, db_path: Path | None = None, memory_limit: int = 500) -> None:
        self._lock = threading.RLock()
        self._mem: OrderedDict[str, _Entry] = OrderedDict()
        self._limit = memory_limit
        self._db = _SqliteBackend(db_path) if db_path else None
        if self._db:
            self._db.mark_interrupted()

    @property
    def persistent(self) -> bool:
        return self._db is not None

    # ---- writes ------------------------------------------------------------------------

    def create(self, job: Job) -> Job:
        with self._lock:
            self._mem[job.id] = _Entry(job=job)
            self._evict()
        if self._db:
            self._db.save_job(job)
        return job

    def update(self, job_id: str, **changes: Any) -> Job:
        with self._lock:
            entry = self._entry(job_id)
            if entry is None:
                raise KeyError(job_id)
            job = entry.job.model_copy(update=changes)
            entry.job = job
        if self._db:
            self._db.save_job(job)
        return job

    def append_event(self, job_id: str, type: str, data: dict[str, Any] | None = None) -> JobEvent:
        with self._lock:
            entry = self._entry(job_id)
            if entry is None:
                raise KeyError(job_id)
            event = JobEvent(seq=len(entry.events) + 1, type=str(type), data=data or {})
            entry.events.append(event)
        if self._db:
            self._db.save_event(job_id, event)
        return event

    # ---- reads -------------------------------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            entry = self._entry(job_id)
            return entry.job if entry else None

    def events(self, job_id: str, after_seq: int = 0) -> list[JobEvent]:
        with self._lock:
            entry = self._entry(job_id)
            if entry is None:
                return []
            return [e for e in entry.events if e.seq > after_seq]

    def close(self) -> None:
        if self._db:
            self._db.close()

    # ---- internals ---------------------------------------------------------------------

    def _entry(self, job_id: str) -> _Entry | None:
        entry = self._mem.get(job_id)
        if entry is not None:
            return entry
        if self._db is None:
            return None
        entry = self._db.load(job_id)
        if entry is not None:
            # Re-hydrate into memory so subsequent reads/writes are cheap.
            self._mem[job_id] = entry
            self._evict()
        return entry

    def _evict(self) -> None:
        """Drop the oldest *finished* jobs once over the memory limit.

        Running jobs are never evicted (their events are only complete in memory). Without
        SQLite, evicted jobs are gone for good, which is the documented trade-off.
        """
        if len(self._mem) <= self._limit:
            return
        for job_id in list(self._mem):
            if len(self._mem) <= self._limit:
                break
            if self._mem[job_id].job.status.is_terminal:
                del self._mem[job_id]
