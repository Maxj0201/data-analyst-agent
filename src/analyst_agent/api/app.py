"""FastAPI serving layer for the data analyst agent.

Run with::

    uvicorn analyst_agent.api.app:app --host 0.0.0.0 --port 8000

Endpoints
---------
``POST /datasets``            upload a CSV/Parquet/SQLite/DuckDB file, returns ``dataset_id``
``GET  /datasets/{id}``       dataset metadata
``POST /ask``                 ``{dataset_id, question}`` -> ``job_id`` (runs in the background)
``GET  /jobs/{id}``           status, progress, cost, full step trace and result
``GET  /jobs/{id}/events``    Server-Sent Events stream of agent steps as they happen
``POST /jobs/{id}/approve``   ``{approve, note?, action_id?}`` resolves a pending approval
``GET  /jobs/{id}/report``    final report as ``text/markdown``
``GET  /artifacts/{path}``    chart images produced by the agent
``GET  /healthz``             liveness probe
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from analyst_agent.core.approval import ApprovalBroker
from analyst_agent.core.config import Settings

from .agent import AgentRunner, core_runner
from .datasets import DatasetStore
from .jobs import JobManager
from .models import (
    ApproveRequest,
    ApproveResponse,
    AskRequest,
    AskResponse,
    DatasetInfo,
    Job,
    JobEvent,
    JobStatus,
    JobView,
)
from .observability import TracerFactory, make_tracer_factory
from .store import JobStore

logger = logging.getLogger(__name__)

ARTIFACT_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
}

# Multipart framing (boundaries, part headers) on top of the file itself.
UPLOAD_OVERHEAD_BYTES = 64 * 1024

SSE_POLL_INTERVAL_S = 0.2
SSE_HEARTBEAT_S = 15.0


def create_app(
    settings: Settings | None = None,
    *,
    runner: AgentRunner | None = None,
    store: JobStore | None = None,
    tracer_factory: TracerFactory | None = None,
    approvals: ApprovalBroker | None = None,
) -> FastAPI:
    settings = settings or Settings()
    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)

    datasets = DatasetStore(settings.uploads_dir, settings.max_upload_mb * 1024 * 1024)
    store = store or JobStore(
        db_path=settings.job_db_path or None, memory_limit=settings.job_memory_limit
    )
    manager = JobManager(
        store=store,
        runner=runner or core_runner,
        tracer_factory=tracer_factory or make_tracer_factory(settings.langfuse_enabled),
        artifacts_dir=settings.artifacts_dir,
        max_workers=settings.max_concurrent_jobs,
        approvals=approvals,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        manager.shutdown()
        store.close()

    app = FastAPI(
        title="Data Analyst Agent API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.jobs = manager
    app.state.datasets = datasets

    app.add_middleware(
        UploadSizeLimit,
        max_bytes=settings.max_upload_mb * 1024 * 1024 + UPLOAD_OVERHEAD_BYTES,
    )

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

    def get_job_or_404(job_id: str) -> Job:
        job = store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        return job

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "job_store": "memory+sqlite" if store.persistent else "memory",
            "langfuse": settings.langfuse_enabled,
        }

    @app.post("/datasets", response_model=DatasetInfo, status_code=201)
    async def upload_dataset(file: UploadFile = File(...)) -> DatasetInfo:  # noqa: B008
        return await datasets.save(file)

    @app.get("/datasets/{dataset_id}", response_model=DatasetInfo)
    def get_dataset(dataset_id: str) -> DatasetInfo:
        info = datasets.info(dataset_id)
        if info is None:
            raise HTTPException(status_code=404, detail="Dataset not found.")
        return info

    @app.post("/ask", response_model=AskResponse, status_code=202)
    def ask(body: AskRequest) -> AskResponse:
        path = datasets.path_for(body.dataset_id)
        if path is None:
            raise HTTPException(status_code=404, detail="Dataset not found.")
        job = manager.submit(dataset_id=body.dataset_id, dataset_path=path, question=body.question)
        return AskResponse(
            job_id=job.id,
            status=job.status,
            events_url=f"/jobs/{job.id}/events",
            job_url=f"/jobs/{job.id}",
        )

    @app.get("/jobs/{job_id}", response_model=JobView)
    def get_job(job_id: str) -> JobView:
        job = get_job_or_404(job_id)
        pending = manager.approvals.pending(job_id)
        return JobView(
            **job.model_dump(),
            duration_seconds=job.duration_s(),
            trace=store.events(job_id),
            pending_approval=pending.to_dict() if pending else None,
        )

    @app.post("/jobs/{job_id}/approve", response_model=ApproveResponse)
    def approve_step(job_id: str, body: ApproveRequest) -> ApproveResponse:
        job = get_job_or_404(job_id)
        if job.status.is_terminal:
            raise HTTPException(status_code=409, detail=f"Job is already {job.status}.")
        action = manager.approvals.resolve(job_id, body.approve, body.note, body.action_id)
        if action is None:
            raise HTTPException(
                status_code=409,
                detail="No matching approval is pending (it may have timed out or been decided).",
            )
        return ApproveResponse(job_id=job_id, action_id=action.id, approved=body.approve)

    @app.get("/jobs/{job_id}/report", response_class=PlainTextResponse)
    def get_report(job_id: str) -> PlainTextResponse:
        job = get_job_or_404(job_id)
        if not job.status.is_terminal:
            raise HTTPException(status_code=409, detail=f"Job is still {job.status}.")
        if job.result is None or not job.result.report_markdown:
            raise HTTPException(status_code=404, detail=job.error or "Job produced no report.")
        return PlainTextResponse(
            job.result.report_markdown, media_type="text/markdown; charset=utf-8"
        )

    @app.get("/jobs/{job_id}/events")
    async def job_events(job_id: str, request: Request) -> StreamingResponse:
        get_job_or_404(job_id)
        try:
            after = int(request.headers.get("last-event-id") or 0)
        except ValueError:
            after = 0
        return StreamingResponse(
            _event_stream(store, job_id, request, after),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/artifacts/{path:path}")
    def get_artifact(path: str) -> FileResponse:
        root = settings.artifacts_dir.resolve()
        target = (root / path).resolve()
        media_type = ARTIFACT_MEDIA_TYPES.get(target.suffix.lower())
        if not target.is_relative_to(root) or media_type is None or not target.is_file():
            raise HTTPException(status_code=404, detail="Artifact not found.")
        return FileResponse(target, media_type=media_type)

    return app


class UploadSizeLimit:
    """Reject oversized ``POST /datasets`` bodies while they stream in.

    Starlette parses (and spools to disk) the whole multipart body before the endpoint runs,
    so a size check in the endpoint comes too late to protect memory and disk. This stops at
    ``Content-Length`` when it is declared, and otherwise as soon as the streamed bytes pass
    the limit.
    """

    def __init__(self, app: ASGIApp, max_bytes: int, paths: tuple[str, ...] = ("/datasets",)):
        self.app = app
        self.max_bytes = max_bytes
        self.paths = paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or scope.get("path") not in self.paths
        ):
            await self.app(scope, receive, send)
            return
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await self._reject(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise HTTPException(status_code=413, detail=self._detail())
            return message

        await self.app(scope, limited_receive, send)

    def _detail(self) -> str:
        mb = (self.max_bytes - UPLOAD_OVERHEAD_BYTES) // (1024 * 1024)
        return f"File exceeds the {mb} MB limit."

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse({"detail": self._detail()}, status_code=413)
        await response(scope, receive, send)


def format_sse(event: JobEvent) -> str:
    payload = json.dumps({"seq": event.seq, "ts": event.ts, **event.data}, default=str)
    return f"id: {event.seq}\nevent: {event.type}\ndata: {payload}\n\n"


async def _event_stream(
    store: JobStore, job_id: str, request: Request, after: int
) -> AsyncIterator[str]:
    """Replay events after ``after``, then tail new ones until the job finishes.

    Ends with an ``end`` event carrying the terminal status. Polling the store keeps this
    independent of where the agent thread runs and works identically for SQLite-backed jobs.
    """
    yield "retry: 2000\n\n"
    last_sent = time.monotonic()
    while True:
        for event in store.events(job_id, after_seq=after):
            after = event.seq
            last_sent = time.monotonic()
            yield format_sse(event)
        job = store.get(job_id)
        if job is None or job.status.is_terminal:
            # Drain anything appended between the read above and the status check.
            for event in store.events(job_id, after_seq=after):
                after = event.seq
                yield format_sse(event)
            status = job.status if job else JobStatus.failed
            yield f"event: end\ndata: {json.dumps({'status': status})}\n\n"
            return
        if await request.is_disconnected():
            return
        if time.monotonic() - last_sent > SSE_HEARTBEAT_S:
            last_sent = time.monotonic()
            yield ": keep-alive\n\n"
        await asyncio.sleep(SSE_POLL_INTERVAL_S)


def __getattr__(name: str) -> Any:
    # ``uvicorn analyst_agent.api.app:app`` — build lazily so importing this module (e.g. in
    # tests) doesn't create directories or open the job database as a side effect.
    if name == "app":
        global app
        app = create_app()
        return app
    raise AttributeError(name)


__all__ = ["create_app", "format_sse"]
