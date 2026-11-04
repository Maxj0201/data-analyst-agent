"""Bridge between the API and the agent loop in ``analyst_agent.core``.

The API only depends on the small :class:`AgentRunner` contract below, which keeps it testable
with a mocked loop and isolates it from the core's internals. :func:`core_runner` is the
production runner: it wires up ``core.AgentLoop`` exactly like ``cli.py`` does and translates
its event hook into the API's event contract.

Event contract (what the job store, SSE stream, frontend and tracing consume)
---------------------------------------------------------------------------
``plan``         ``{"plan": str, "turn"?}``
``llm_call``     ``{"model", "purpose", "input_tokens", "output_tokens", "cache_read_tokens",
                  "cache_write_tokens", "usd", "latency_ms", "stop_reason"}``
                  (emitted *after* each completed LLM request)
``tool_call``    ``{"id", "name", "input", "turn"?}``
``tool_result``  ``{"id", "name", "ok": bool, "summary": str}``
``critic``       ``{"verdict": "pass"|"revise", "issues": [...], "suggested_fix": str, "round"}``
``stop``         ``{"status", "detail"}`` (loop ended early or with caveats)
``progress``     ``{"turn": int, "max_turns": int}``

Events carrying ``turn``/``max_turns`` drive the job's progress fraction. Unknown types are
passed through to the stream untouched.
"""

from __future__ import annotations

import itertools
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

EmitFn = Callable[[str, dict[str, Any]], None]


@dataclass
class AgentOutcome:
    status: str = "completed"
    """``completed`` | ``partial`` (e.g. budget/turn limit hit) | ``failed``."""
    report_markdown: str = ""
    key_numbers: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    """Paths of generated files (absolute or relative to the artifacts dir / cwd)."""
    cost: dict[str, Any] | None = None
    """Optional authoritative totals; otherwise the API sums the ``llm_call`` events."""
    error: str | None = None
    agent_status: str | None = None
    """The loop's own status string (e.g. ``budget_exceeded``), kept for display."""


class AgentRunner(Protocol):
    def __call__(self, *, dataset_path: Path, question: str, emit: EmitFn) -> AgentOutcome: ...


# ---- event translation ---------------------------------------------------------------------


class EventTranslator:
    """Adapts ``AgentLoop``'s ``on_event(kind, payload)`` hook to the API event contract.

    The loop emits ``tool_call`` without an id and ``tool_result`` with ``is_error``; tool calls
    within a turn execute sequentially, so results are paired with calls FIFO per tool name.
    """

    def __init__(self, emit: EmitFn, max_turns: int | None = None) -> None:
        self._emit = emit
        self._max_turns = max_turns
        self._ids = itertools.count(1)
        self._pending: dict[str, deque[str]] = {}

    def __call__(self, kind: str, payload: dict[str, Any]) -> None:
        data = dict(payload)
        if self._max_turns and "turn" in data:
            data.setdefault("max_turns", self._max_turns)
        if kind == "tool_call":
            call_id = str(data.get("id") or f"call_{next(self._ids)}")
            data["id"] = call_id
            self._pending.setdefault(str(data.get("name")), deque()).append(call_id)
        elif kind == "tool_result":
            if "id" not in data:
                queue = self._pending.get(str(data.get("name")))
                data["id"] = queue.popleft() if queue else f"call_{next(self._ids)}"
            if "ok" not in data:
                data["ok"] = not data.pop("is_error", False)
        self._emit(kind, data)


class EventedLLM:
    """Wraps a ``core.llm.LLM`` and emits one ``llm_call`` event per completed request.

    All calls still go through the wrapped client (``core/llm.py``); this only observes them,
    which is how every LLM call becomes a Langfuse generation span without the core knowing.
    """

    def __init__(
        self,
        inner: Any,
        emit: EmitFn,
        cost_fn: Callable[[str, Any], float] | None = None,
    ) -> None:
        self._inner = inner
        self._emit = emit
        self._cost_fn = cost_fn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def create(self, **kwargs: Any) -> Any:
        started = time.perf_counter()
        response = self._inner.create(**kwargs)
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        usage = getattr(response, "usage", None)
        model = getattr(response, "model", None) or kwargs.get("model") or ""

        def tokens(name: str) -> int:
            return int(getattr(usage, name, 0) or 0)

        usd = 0.0
        if self._cost_fn is not None and usage is not None:
            try:
                usd = float(self._cost_fn(model, usage))
            except Exception:  # noqa: BLE001 - pricing gaps must not fail the call
                usd = 0.0
        self._emit(
            "llm_call",
            {
                "model": model,
                "purpose": kwargs.get("role", "agent"),
                "input_tokens": tokens("input_tokens"),
                "output_tokens": tokens("output_tokens"),
                "cache_read_tokens": tokens("cache_read_input_tokens"),
                "cache_write_tokens": tokens("cache_creation_input_tokens"),
                "usd": round(usd, 6),
                "latency_ms": latency_ms,
                "stop_reason": getattr(response, "stop_reason", None),
            },
        )
        return response


# ---- default runner: analyst_agent.core.AgentLoop ------------------------------------------


def core_runner(*, dataset_path: Path, question: str, emit: EmitFn) -> AgentOutcome:
    """Run the real agent loop over one uploaded CSV (mirrors ``analyst_agent.cli.ask``)."""
    try:
        from analyst_agent.core import AgentLoop, LLMClient, Settings
        from analyst_agent.core.llm import estimate_cost_usd
        from analyst_agent.tools import (
            ToolContext,
            build_default_registry,
            create_sandbox,
            table_name_for,
        )
    except ImportError as exc:  # pragma: no cover - only without the core installed
        raise RuntimeError(f"Agent core is not available: {exc}") from exc

    settings = Settings()
    datasets = {table_name_for(dataset_path): dataset_path}
    # One sandbox per job: jobs run concurrently on a thread pool, so the process-wide
    # module-level sandbox (used by the CLI) must not be shared between them.
    sb = create_sandbox(datasets, artifacts_dir=settings.artifacts_dir)
    ctx = ToolContext(tables=sb.tables, artifacts_dir=settings.artifacts_dir, sandbox=sb)
    loop = AgentLoop(
        EventedLLM(LLMClient(settings), emit, cost_fn=estimate_cost_usd),
        build_default_registry(),
        ctx,
        settings,
        on_event=EventTranslator(emit, max_turns=settings.max_turns),
    )
    try:
        return coerce_outcome(loop.run(question))
    finally:
        sb.close()


# ---- result normalisation ------------------------------------------------------------------

_COMPLETED_STATUSES = {"completed", "completed_with_issues", "succeeded", "success", "ok"}
_PARTIAL_STATUSES = {"partial", "budget_exceeded", "max_turns", "stopped"}


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, Mapping) and name in obj:
            return obj[name]
        if not isinstance(obj, Mapping) and hasattr(obj, name):
            return getattr(obj, name)
    return default


def _as_dict(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None) or getattr(value, "model_dump", None)
    if callable(to_dict):
        out = to_dict()
        if isinstance(out, Mapping):
            return dict(out)
    return None


def coerce_outcome(result: Any) -> AgentOutcome:
    """Normalise ``core.AgentResult`` (or a similar object/dict/str) into an AgentOutcome."""
    if isinstance(result, AgentOutcome):
        return result
    if isinstance(result, str):
        return AgentOutcome(report_markdown=result)

    raw = _get(result, "status", default="completed") or "completed"
    raw_status = str(getattr(raw, "value", raw)).lower()  # enums -> their value
    if raw_status in _COMPLETED_STATUSES:
        status = "completed"
    elif raw_status in _PARTIAL_STATUSES:
        status = "partial"
    else:  # refused, error, failed, unknown
        status = "failed"

    state = _get(result, "state")
    artifacts = _get(result, "artifacts") or _get(state, "artifacts") or []
    detail = _get(result, "error", "detail") or _get(state, "detail") or None
    report = _get(result, "report_markdown", "report", default="") or ""

    return AgentOutcome(
        status=status,
        report_markdown=str(report),
        key_numbers=dict(_get(result, "key_numbers") or {}),
        artifacts=[str(p) for p in artifacts],
        cost=_as_dict(_get(result, "cost")),
        error=str(detail) if detail and status != "completed" else None,
        agent_status=raw_status,
    )
