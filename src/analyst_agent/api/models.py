"""Pydantic models shared by the job store, the API and the frontend."""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class JobStatus(StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    partial = "partial"  # agent stopped early (e.g. token budget) but produced a result
    failed = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (JobStatus.succeeded, JobStatus.partial, JobStatus.failed)


class EventType(StrEnum):
    """Step types streamed over SSE. The agent core may emit any of the first six."""

    plan = "plan"
    llm_call = "llm_call"
    tool_call = "tool_call"
    tool_result = "tool_result"
    critic = "critic"
    progress = "progress"
    # Emitted by the API itself:
    status = "status"
    error = "error"
    final = "final"


class JobEvent(BaseModel):
    seq: int
    type: str
    ts: float = Field(default_factory=time.time)
    data: dict[str, Any] = Field(default_factory=dict)


class Cost(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    usd: float = 0.0
    llm_calls: int = 0


class Progress(BaseModel):
    phase: str = "queued"
    steps: int = 0
    turn: int | None = None
    max_turns: int | None = None
    fraction: float = 0.0


class Artifact(BaseModel):
    path: str
    """Path relative to the artifacts directory."""
    url: str


class JobResult(BaseModel):
    report_markdown: str = ""
    key_numbers: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[Artifact] = Field(default_factory=list)
    agent_status: str | None = None
    """Raw status string reported by the agent loop (e.g. ``budget_exceeded``)."""


class Job(BaseModel):
    id: str
    dataset_id: str
    question: str
    status: JobStatus = JobStatus.queued
    created_at: float = Field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    progress: Progress = Field(default_factory=Progress)
    cost: Cost = Field(default_factory=Cost)
    result: JobResult | None = None
    error: str | None = None

    def duration_s(self) -> float | None:
        if self.started_at is None:
            return None
        return round((self.finished_at or time.time()) - self.started_at, 3)


# ---- request / response bodies -------------------------------------------------------------


class DatasetInfo(BaseModel):
    dataset_id: str
    filename: str
    """Name as uploaded."""
    stored_name: str
    """Sanitised name on disk; its stem becomes the SQL table name the agent sees."""
    size_bytes: int
    columns: list[str]
    created_at: float


class AskRequest(BaseModel):
    dataset_id: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1, max_length=4000)


class AskResponse(BaseModel):
    job_id: str
    status: JobStatus
    events_url: str
    job_url: str


class JobView(Job):
    """``GET /jobs/{id}`` payload: the job plus its full step trace."""

    duration_seconds: float | None = None
    trace: list[JobEvent] = Field(default_factory=list)
