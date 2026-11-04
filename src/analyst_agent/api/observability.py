"""Per-job tracing for the API: Langfuse (``AGENT_LANGFUSE_ENABLED``) and/or saved span trees
(``AGENT_TRACE_STORE_ENABLED``).

Each job gets a :class:`~analyst_agent.core.tracing.TraceRecorder` fed by the job's event
stream, which builds the run → turn → llm_call / tool_call / critic / approval / route tree
(see ``core/tracing.py``). A :class:`~analyst_agent.core.langfuse_export.LangfuseSink` mirrors it
into one Langfuse trace live; on finish the tree is optionally saved as
``<AGENT_TRACES_DIR>/<job_id>.json`` for ``analyst-agent trace show <job_id|run_id>``.

The agent core doesn't import Langfuse, and tracing can never break a job: the recorder catches
sink failures and :class:`JobTracer` catches its own.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol

from analyst_agent.core.langfuse_export import LangfuseSink, make_langfuse_client
from analyst_agent.core.tracing import SpanSink, TraceRecorder, save_trace

logger = logging.getLogger(__name__)


class JobTracer(Protocol):
    def on_event(self, type: str, data: dict[str, Any]) -> None: ...
    def finish(self, *, status: str, output: Any = None, error: str | None = None) -> None: ...


class TracerFactory(Protocol):
    def __call__(self, *, job_id: str, dataset_id: str, question: str) -> JobTracer: ...
    def shutdown(self) -> None: ...


class NoopTracer:
    def on_event(self, type: str, data: dict[str, Any]) -> None:
        pass

    def finish(self, *, status: str, output: Any = None, error: str | None = None) -> None:
        pass


class NoopTracerFactory:
    def __call__(self, *, job_id: str, dataset_id: str, question: str) -> JobTracer:
        return NoopTracer()

    def shutdown(self) -> None:
        pass


class RecorderTracer:
    """A job's span tree, mirrored to its sinks and optionally saved on finish."""

    def __init__(self, recorder: TraceRecorder, *, store_dir: Path | None, job_id: str) -> None:
        self.recorder = recorder
        self._store_dir = store_dir
        self._job_id = job_id

    def on_event(self, type: str, data: dict[str, Any]) -> None:
        self.recorder.on_event(type, data)  # never raises

    def finish(self, *, status: str, output: Any = None, error: str | None = None) -> None:
        try:
            root = self.recorder.finish(status=str(status), output=output, error=error)
            if self._store_dir is not None:
                path = save_trace(self._store_dir, root, name=self._job_id)
                logger.info("trace saved", extra={"trace_path": str(path)})
        except Exception:  # noqa: BLE001 - tracing must never fail a job
            logger.exception("Tracing failed while finishing job")


class RecorderTracerFactory:
    """Builds a :class:`RecorderTracer` per job.

    ``client`` is a Langfuse client (or a fake in tests); ``None`` disables Langfuse.
    ``store_dir`` enables saving span trees.
    """

    def __init__(self, client: Any | None = None, *, store_dir: Path | None = None) -> None:
        self._client = client
        self._store_dir = store_dir

    @property
    def langfuse_enabled(self) -> bool:
        return self._client is not None

    def __call__(self, *, job_id: str, dataset_id: str, question: str) -> JobTracer:
        sinks: list[SpanSink] = []
        if self._client is not None:
            sinks.append(LangfuseSink(self._client, root_name="analyst-job"))
        try:
            recorder = TraceRecorder(
                sinks=sinks,
                metadata={"job_id": job_id, "dataset_id": dataset_id},
                question=question,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Could not start trace; continuing without tracing")
            return NoopTracer()
        return RecorderTracer(recorder, store_dir=self._store_dir, job_id=job_id)

    def shutdown(self) -> None:
        if self._client is None:
            return
        try:
            self._client.shutdown()
        except Exception:  # noqa: BLE001
            logger.exception("Langfuse shutdown failed")


class LangfuseTracerFactory(RecorderTracerFactory):
    """Langfuse-backed factory; creates the SDK client from ``LANGFUSE_*`` when not given."""

    def __init__(self, client: Any | None = None, *, store_dir: Path | None = None) -> None:
        if client is None:
            from langfuse import Langfuse  # imported lazily: optional dependency

            client = Langfuse()  # LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST
        super().__init__(client, store_dir=store_dir)


def make_tracer_factory(enabled: bool, store_dir: Path | None = None) -> TracerFactory:
    """``enabled`` = ``AGENT_LANGFUSE_ENABLED``; ``store_dir`` set when
    ``AGENT_TRACE_STORE_ENABLED``. Neither → no tracing at all."""
    client = make_langfuse_client() if enabled else None
    if client is None and store_dir is None:
        return NoopTracerFactory()
    if client is not None:
        return LangfuseTracerFactory(client, store_dir=store_dir)
    return RecorderTracerFactory(None, store_dir=store_dir)
