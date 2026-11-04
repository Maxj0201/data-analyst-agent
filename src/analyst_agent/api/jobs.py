"""Background execution of agent jobs.

Jobs run on a bounded thread pool (the agent loop and the Anthropic SDK client are
synchronous). Each step the agent emits is appended to the job store, folded into the job's
progress/cost, and forwarded to the tracer, so ``GET /jobs/{id}`` and the SSE stream see it
immediately.
"""

from __future__ import annotations

import inspect
import logging
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from analyst_agent.core.approval import ApprovalBroker

from .agent import AgentOutcome, AgentRunner
from .models import Artifact, Cost, EventType, Job, JobResult, JobStatus, Progress
from .observability import TracerFactory
from .store import JobStore

logger = logging.getLogger(__name__)

_COST_ALIASES = {
    "input_tokens": ("input_tokens",),
    "output_tokens": ("output_tokens",),
    "cache_read_tokens": ("cache_read_tokens", "cache_read_input_tokens"),
    "cache_write_tokens": ("cache_write_tokens", "cache_creation_input_tokens"),
    "usd": ("usd", "total_usd", "cost_usd", "estimated_usd"),
    "llm_calls": ("llm_calls", "calls"),
}


def new_job_id() -> str:
    return "job_" + uuid.uuid4().hex[:16]


class JobManager:
    def __init__(
        self,
        *,
        store: JobStore,
        runner: AgentRunner,
        tracer_factory: TracerFactory,
        artifacts_dir: Path,
        max_workers: int,
        approvals: ApprovalBroker | None = None,
    ) -> None:
        self.store = store
        self.runner = runner
        self.tracer_factory = tracer_factory
        self.artifacts_dir = artifacts_dir
        self.approvals = approvals or ApprovalBroker()
        self._pass_approver = _accepts_kwarg(runner, "approver")
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="agent-job")

    def submit(self, *, dataset_id: str, dataset_path: Path, question: str) -> Job:
        job = self.store.create(Job(id=new_job_id(), dataset_id=dataset_id, question=question))
        self.store.append_event(job.id, EventType.status, {"status": JobStatus.queued})
        self._executor.submit(self._run, job.id, dataset_path)
        return job

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
        self.approvals.close()
        self.tracer_factory.shutdown()

    # ---- worker ------------------------------------------------------------------------

    def _run(self, job_id: str, dataset_path: Path) -> None:
        job = self.store.update(
            job_id,
            status=JobStatus.running,
            started_at=time.time(),
            progress=Progress(phase="starting"),
        )
        self.store.append_event(job_id, EventType.status, {"status": JobStatus.running})
        tracer = self.tracer_factory(
            job_id=job_id, dataset_id=job.dataset_id, question=job.question
        )

        def emit(type: str, data: dict[str, Any] | None = None) -> None:
            data = dict(data or {})
            type = str(getattr(type, "value", type))
            self.store.append_event(job_id, type, data)
            self._fold(job_id, type, data)
            tracer.on_event(type, data)

        try:
            extra: dict[str, Any] = {}
            if self._pass_approver:
                extra["approver"] = self.approvals.for_job(job_id)
            outcome = self.runner(
                dataset_path=dataset_path, question=job.question, emit=emit, **extra
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the client, never raised
            logger.exception("Job %s failed", job_id)
            self._fail(job_id, f"{type(exc).__name__}: {exc}")
            tracer.finish(status="failed", error=str(exc))
            return

        try:
            self._complete(job_id, outcome)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Job %s failed while saving its result", job_id)
            self._fail(job_id, f"Could not save result: {exc}")
            tracer.finish(status="failed", error=str(exc))
            return
        final = self.store.get(job_id)
        assert final is not None
        tracer.finish(
            status=final.status,
            output=outcome.report_markdown,
            error=final.error,
        )

    def _fold(self, job_id: str, type: str, data: dict[str, Any]) -> None:
        """Update progress (and running cost for ``llm_call``) from one step event."""
        job = self.store.get(job_id)
        if job is None:
            return
        progress = job.progress.model_copy()
        progress.steps += 1
        progress.phase = type
        if isinstance(data.get("turn"), int):
            progress.turn = data["turn"]
        if isinstance(data.get("max_turns"), int) and data["max_turns"] > 0:
            progress.max_turns = data["max_turns"]
        if progress.turn is not None and progress.max_turns:
            # Never report 100% before the job is actually finished.
            progress.fraction = min(0.95, progress.turn / progress.max_turns)
        changes: dict[str, Any] = {"progress": progress}
        if type == EventType.llm_call:
            cost = job.cost.model_copy()
            cost.input_tokens += int(data.get("input_tokens") or 0)
            cost.output_tokens += int(data.get("output_tokens") or 0)
            cost.cache_read_tokens += int(data.get("cache_read_tokens") or 0)
            cost.cache_write_tokens += int(data.get("cache_write_tokens") or 0)
            cost.usd += float(data.get("usd") or 0.0)
            cost.llm_calls += 1
            changes["cost"] = cost
        self.store.update(job_id, **changes)

    def _complete(self, job_id: str, outcome: AgentOutcome) -> None:
        job = self.store.get(job_id)
        assert job is not None
        cost = _merge_cost(job.cost, outcome.cost)
        artifacts = [
            a for a in (self._register_artifact(job_id, p) for p in outcome.artifacts) if a
        ]
        report = outcome.report_markdown or ""
        for original, artifact in artifacts:
            report = report.replace(f"]({original})", f"]({artifact.url})")
        status = {
            "completed": JobStatus.succeeded,
            "partial": JobStatus.partial,
            "failed": JobStatus.failed,
        }.get(outcome.status, JobStatus.succeeded)

        result = JobResult(
            report_markdown=report,
            key_numbers=outcome.key_numbers,
            artifacts=[a for _, a in artifacts],
            agent_status=outcome.agent_status or outcome.status,
        )
        # The final event is appended *before* the status flips to terminal so that SSE
        # readers, which stop once they observe a terminal status, never miss it.
        self.store.append_event(
            job_id,
            EventType.final,
            {
                "status": status,
                "cost": cost.model_dump(),
                "artifacts": [a.model_dump() for a in result.artifacts],
                "key_numbers": result.key_numbers,
                "error": outcome.error,
            },
        )
        progress = job.progress.model_copy(update={"phase": "done", "fraction": 1.0})
        self.store.update(
            job_id,
            status=status,
            finished_at=time.time(),
            result=result,
            cost=cost,
            progress=progress,
            error=outcome.error,
        )

    def _fail(self, job_id: str, message: str) -> None:
        job = self.store.get(job_id)
        progress = job.progress.model_copy(update={"phase": "failed"}) if job else Progress()
        self.store.append_event(job_id, EventType.error, {"message": message})
        self.store.append_event(
            job_id, EventType.final, {"status": JobStatus.failed, "error": message}
        )
        self.store.update(
            job_id,
            status=JobStatus.failed,
            finished_at=time.time(),
            error=message,
            progress=progress,
        )

    def _register_artifact(self, job_id: str, raw: str) -> tuple[str, Artifact] | None:
        """Resolve an artifact path from the agent to a file inside the artifacts dir.

        Files the agent wrote elsewhere are copied into ``<artifacts>/<job_id>/`` so the
        ``/artifacts`` route never has to serve from outside its root.
        """
        root = self.artifacts_dir.resolve()
        path = Path(raw)
        candidates = [path] if path.is_absolute() else [root / path, Path.cwd() / path]
        if not path.is_absolute() and path.parts and path.parts[0] == self.artifacts_dir.name:
            candidates.insert(0, root / Path(*path.parts[1:]))
        found = next((c.resolve() for c in candidates if c.is_file()), None)
        if found is None:
            logger.warning("Job %s: artifact %r not found, skipping", job_id, raw)
            return None
        if not found.is_relative_to(root):
            dest = root / job_id / found.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(found, dest)
            found = dest
        rel = found.relative_to(root).as_posix()
        return raw, Artifact(path=rel, url=f"/artifacts/{rel}")


def _accepts_kwarg(fn: Any, name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == name or p.kind is inspect.Parameter.VAR_KEYWORD for p in params)


def _merge_cost(running: Cost, reported: dict[str, Any] | None) -> Cost:
    if not reported:
        return running
    merged = running.model_copy()
    for field, aliases in _COST_ALIASES.items():
        for alias in aliases:
            if reported.get(alias) is not None:
                cast = float if field == "usd" else int
                setattr(merged, field, cast(reported[alias]))
                break
    return merged
