"""Langfuse tracing, behind the ``AGENT_LANGFUSE_ENABLED`` feature flag.

One Langfuse trace per job. The job's root observation is an ``agent`` span; every agent step
event becomes a child observation:

* ``llm_call``     -> ``generation`` (model, token usage, USD cost)
* ``tool_call``    -> ``tool`` span, opened on the call and closed by the matching ``tool_result``
* ``plan``/``critic`` -> ``span`` / ``evaluator`` observations

Tracing is driven entirely by the event stream, so the agent core doesn't import Langfuse and
tracing can never break a job: every SDK call is wrapped and failures are logged once.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Protocol

logger = logging.getLogger(__name__)

_MAX_FIELD_CHARS = 20_000


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


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_FIELD_CHARS:
        return value[:_MAX_FIELD_CHARS] + "…[truncated]"
    return value


class LangfuseJobTracer:
    def __init__(self, client: Any, *, job_id: str, dataset_id: str, question: str) -> None:
        self._client = client
        self._lock = threading.Lock()
        self._open_tools: dict[str, Any] = {}
        self._root = client.start_observation(
            name="analyst-job",
            as_type="agent",
            input={"question": question, "dataset_id": dataset_id},
            metadata={"job_id": job_id, "dataset_id": dataset_id},
        )

    def on_event(self, type: str, data: dict[str, Any]) -> None:
        try:
            with self._lock:
                self._handle(type, data)
        except Exception:  # noqa: BLE001 - tracing must never break a job
            logger.exception("Langfuse tracing failed for event %s", type)

    def _handle(self, type: str, data: dict[str, Any]) -> None:
        root = self._root
        if type == "llm_call":
            usage = {
                k: int(data[src])
                for k, src in (
                    ("input", "input_tokens"),
                    ("output", "output_tokens"),
                    ("cache_read_input_tokens", "cache_read_tokens"),
                    ("cache_creation_input_tokens", "cache_write_tokens"),
                )
                if data.get(src) is not None
            }
            gen = root.start_observation(
                name=f"llm:{data.get('purpose') or 'call'}",
                as_type="generation",
                model=data.get("model"),
                input=_clip(data.get("input")),
                output=_clip(data.get("output")),
                usage_details=usage or None,
                cost_details={"total": float(data["usd"])} if data.get("usd") is not None else None,
                metadata={"latency_ms": data.get("latency_ms")},
            )
            gen.end()
        elif type == "tool_call":
            key = str(data.get("id") or data.get("name"))
            self._open_tools[key] = root.start_observation(
                name=f"tool:{data.get('name')}", as_type="tool", input=_clip(data.get("input"))
            )
        elif type == "tool_result":
            key = str(data.get("id") or data.get("name"))
            span = self._open_tools.pop(key, None) or root.start_observation(
                name=f"tool:{data.get('name')}", as_type="tool"
            )
            ok = data.get("ok", True)
            span.update(
                output=_clip(data.get("summary")),
                level="DEFAULT" if ok else "ERROR",
                status_message=None if ok else str(data.get("error") or data.get("summary")),
            )
            span.end()
        elif type == "plan":
            root.start_observation(
                name="plan", as_type="span", output=_clip(data.get("plan"))
            ).end()
        elif type == "critic":
            root.start_observation(
                name="critic",
                as_type="evaluator",
                output=data,
                level="WARNING" if data.get("verdict") == "revise" else "DEFAULT",
            ).end()

    def finish(self, *, status: str, output: Any = None, error: str | None = None) -> None:
        try:
            with self._lock:
                for span in self._open_tools.values():
                    span.update(level="WARNING", status_message="tool span never closed")
                    span.end()
                self._open_tools.clear()
                self._root.update(
                    output=_clip(output),
                    level="ERROR" if status == "failed" else "DEFAULT",
                    status_message=error,
                    metadata={"status": status},
                )
                self._root.end()
            self._client.flush()
        except Exception:  # noqa: BLE001
            logger.exception("Langfuse tracing failed while finishing job")


class LangfuseTracerFactory:
    def __init__(self, client: Any | None = None) -> None:
        if client is None:
            from langfuse import Langfuse  # imported lazily: optional dependency

            client = Langfuse()  # LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST
        self._client = client

    def __call__(self, *, job_id: str, dataset_id: str, question: str) -> JobTracer:
        try:
            return LangfuseJobTracer(
                self._client, job_id=job_id, dataset_id=dataset_id, question=question
            )
        except Exception:  # noqa: BLE001
            logger.exception("Could not start Langfuse trace; continuing without tracing")
            return NoopTracer()

    def shutdown(self) -> None:
        try:
            self._client.shutdown()
        except Exception:  # noqa: BLE001
            logger.exception("Langfuse shutdown failed")


def make_tracer_factory(enabled: bool) -> TracerFactory:
    if not enabled:
        return NoopTracerFactory()
    try:
        return LangfuseTracerFactory()
    except Exception:  # noqa: BLE001
        logger.exception("AGENT_LANGFUSE_ENABLED is set but Langfuse could not be initialised")
        return NoopTracerFactory()
