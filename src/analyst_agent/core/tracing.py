"""Span tree for one agent run, built from the loop's event stream.

The loop (``on_event``) and :class:`EventedLLM` (one ``llm_call`` event per completed request)
already describe everything a run does. :class:`TraceRecorder` folds that stream into a tree::

    run
    ├─ route                 (AGENT_ROUTING_ENABLED; the router's llm_call nested inside)
    ├─ retrieval             (AGENT_SEMANTIC_LAYER_ENABLED)
    ├─ turn 1
    │  ├─ llm_call agent
    │  ├─ plan
    │  ├─ tool_call run_sql
    │  │  └─ approval        (AGENT_APPROVAL_ENABLED; the gate runs before the tool executes)
    │  ├─ critic             (the critic's llm_call nested inside)
    │  └─ escalation
    ├─ turn 2 …
    └─ llm_call report_writer

Every ``llm_call`` span carries model, tokens, USD cost, latency, the version of the prompt its
role uses and the routed tier at that moment; the run span carries totals, the prompt versions
and the routed / final tier. Containers' costs are the sum of their descendants
(:meth:`Span.total_cost`).

The tree is the single source for every trace consumer: :class:`SpanSink` implementations mirror
it live (Langfuse, ``core/langfuse_export.py``), and :func:`save_trace` writes it to JSON for
``analyst-agent trace show`` (:func:`render_tree`). The recorder never raises into the agent: a
failing sink is logged and skipped.

Accepted events are the loop's raw ones (``tool_result`` with ``is_error``, no ids) and the API's
translated ones (``ok``, ``id``); see ``api/agent.py`` for the contract.
"""

from __future__ import annotations

import itertools
import json
import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

TRACE_FORMAT_VERSION = 1
_MAX_FIELD_CHARS = 20_000

# LLM role (``create(role=...)``) -> name of the prompt it is sent (``core.prompts.PROMPTS``).
ROLE_PROMPTS = {
    "agent": "planner",
    "critic": "critic",
    "router": "router",
    "report_writer": "report_writer",
}


def new_run_id() -> str:
    return "run_" + uuid.uuid4().hex[:16]


def clip(value: Any, limit: int = _MAX_FIELD_CHARS) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…[truncated]"
    return value


# ---- span model ----------------------------------------------------------------------------


@dataclass
class Span:
    id: str
    kind: str
    """``run`` | ``turn`` | ``llm_call`` | ``tool_call`` | ``critic`` | ``approval`` | ``route``
    | ``plan`` | ``escalation`` | ``retrieval``"""
    name: str
    start: float  # epoch seconds
    end: float | None = None
    status: str = "ok"  # ok | warning | error
    status_message: str | None = None
    input: Any = None
    output: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    children: list[Span] = field(default_factory=list)

    @property
    def duration(self) -> float | None:
        return None if self.end is None else max(0.0, self.end - self.start)

    @property
    def own_cost(self) -> float:
        return float(self.metadata.get("cost_usd") or 0.0) if self.kind == "llm_call" else 0.0

    def total_cost(self) -> float:
        return self.own_cost + sum(c.total_cost() for c in self.children)

    def walk(self) -> Iterator[Span]:
        yield self
        for c in self.children:
            yield from c.walk()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "start": self.start,
            "end": self.end,
            "duration_s": None if self.duration is None else round(self.duration, 6),
            "cost_usd": round(self.total_cost(), 6),
            "status": self.status,
            "status_message": self.status_message,
            "input": self.input,
            "output": self.output,
            "metadata": self.metadata,
            "children": [c.to_dict() for c in self.children],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Span:
        return cls(
            id=str(d.get("id", "")),
            kind=str(d.get("kind", "span")),
            name=str(d.get("name", "")),
            start=float(d.get("start") or 0.0),
            end=None if d.get("end") is None else float(d["end"]),
            status=str(d.get("status") or "ok"),
            status_message=d.get("status_message"),
            input=d.get("input"),
            output=d.get("output"),
            metadata=dict(d.get("metadata") or {}),
            children=[cls.from_dict(c) for c in d.get("children") or []],
        )


class SpanSink(Protocol):
    """Live consumer of the tree (e.g. Langfuse). Called under the recorder's lock."""

    def span_started(self, span: Span, parent: Span | None) -> None: ...
    def span_ended(self, span: Span) -> None: ...
    def close(self, root: Span) -> None: ...


# ---- recorder ------------------------------------------------------------------------------


class TraceRecorder:
    """Builds the span tree for one run from ``(event_type, data)`` pairs. Thread-safe."""

    def __init__(
        self,
        *,
        sinks: list[SpanSink] | None = None,
        metadata: dict[str, Any] | None = None,
        question: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._sinks = list(sinks or [])
        self._clock = clock
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self.root = Span(
            id="s0",
            kind="run",
            name="run",
            start=clock(),
            input={"question": question} if question is not None else None,
            metadata=dict(metadata or {}),
        )
        self._turn: Span | None = None
        self._turn_explicit = False  # opened by a ``turn`` event (vs. by an agent llm_call)
        self._turn_count = 0
        self._route: Span | None = None
        self._critic: Span | None = None
        self._tools: dict[str, Span] = {}
        self._tool_fifo: dict[str, deque[str]] = {}
        self._approvals: dict[str, Span] = {}
        self._tier: str | None = None
        self._finished = False
        self._notify("span_started", self.root, None)

    # ---- public ------------------------------------------------------------------------

    @property
    def run_id(self) -> str | None:
        return self.root.metadata.get("run_id")

    @property
    def finished(self) -> bool:
        return self._finished

    def on_event(self, type: str, data: dict[str, Any] | None = None) -> None:
        try:
            with self._lock:
                if not self._finished:
                    self._handle(str(getattr(type, "value", type)), dict(data or {}))
        except Exception:  # noqa: BLE001 - tracing must never break a run
            log.exception("trace recorder failed on event %s", type)

    def finish(self, *, status: str, output: Any = None, error: str | None = None) -> Span:
        """Close every open span and the run. Idempotent; returns the root span."""
        with self._lock:
            if self._finished:
                return self.root
            self._finished = True
            now = self._clock()
            for span in list(self._approvals.values()):
                self._close(span, now, "warning", "approval never decided")
            for span in list(self._tools.values()):
                self._close(span, now, "warning", "tool span never closed")
            self._approvals.clear()
            self._tools.clear()
            if self._critic is not None:
                self._close(self._critic, now, "warning", "critic never returned a verdict")
            if self._route is not None:
                self._close(self._route, now, "warning", "router never returned a tier")
            self._close_turn(now)
            root = self.root
            root.output = clip(output) if output is not None else root.output
            root.metadata.update(self._totals())
            root.metadata["status"] = status
            failed = status in ("failed", "error")
            self._close(root, now, "error" if failed else "ok", error)
            for sink in self._sinks:
                self._call(sink, "close", root)
            return root

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return trace_document(self.root)

    # ---- event handling ----------------------------------------------------------------

    def _handle(self, type: str, data: dict[str, Any]) -> None:
        handler = getattr(self, f"_on_{type}", None)
        if handler is not None:
            handler(data)

    def _on_run_start(self, data: dict[str, Any]) -> None:
        md = self.root.metadata
        for key in ("run_id", "model", "tables", "max_turns", "token_budget", "flags"):
            if data.get(key) is not None:
                md[key] = data[key]
        if data.get("prompts"):
            md["prompt_versions"] = {
                name: p.get("version") if isinstance(p, dict) else p
                for name, p in data["prompts"].items()
            }
        if data.get("question") is not None and self.root.input is None:
            self.root.input = {"question": data["question"]}

    def _on_turn(self, data: dict[str, Any]) -> None:
        now = self._clock()
        self._close_turn(now)
        self._open_turn(now, data)
        self._turn_explicit = True

    def _on_llm_call(self, data: dict[str, Any]) -> None:
        now = self._clock()
        latency_s = float(data.get("latency_ms") or 0.0) / 1000
        start = now - latency_s
        purpose = str(data.get("purpose") or "call")
        if purpose == "router":
            if self._route is None:
                self._route = self._open("route", "route", start, self.root)
            parent = self._route
        elif purpose == "critic":
            if self._critic is None:
                self._critic = self._open("critic", "critic", start, self._container())
            parent = self._critic
        elif purpose == "agent":
            # Producers without ``turn`` events (older runners): each agent call opens a turn.
            implicit_next = not self._turn_explicit and any(
                c.kind == "llm_call" and c.metadata.get("purpose") == "agent"
                for c in (self._turn.children if self._turn else [])
            )
            if self._turn is None or implicit_next:
                self._close_turn(start)
                self._open_turn(start, {})
                self._turn_explicit = False
            parent = self._turn
        else:
            parent = self._container()
        assert parent is not None
        model = data.get("model") or None
        md: dict[str, Any] = {
            "model": model,
            "purpose": purpose,
            "input_tokens": int(data.get("input_tokens") or 0),
            "output_tokens": int(data.get("output_tokens") or 0),
            "cache_read_tokens": int(data.get("cache_read_tokens") or 0),
            "cache_write_tokens": int(data.get("cache_write_tokens") or 0),
            "cost_usd": float(data.get("usd") or 0.0),
            "latency_ms": data.get("latency_ms"),
            "stop_reason": data.get("stop_reason"),
        }
        prompt = ROLE_PROMPTS.get(purpose)
        versions = self.root.metadata.get("prompt_versions") or {}
        if prompt:
            md["prompt"] = prompt
            if prompt in versions:
                md["prompt_version"] = versions[prompt]
        if self._tier and purpose in ("agent", "report_writer"):
            md["tier"] = self._tier
        span = self._open("llm_call", f"llm:{purpose}", start, parent, metadata=md)
        refused = data.get("stop_reason") == "refusal"
        self._close(span, now, "warning" if refused else "ok", "refusal" if refused else None)

    def _on_plan(self, data: dict[str, Any]) -> None:
        self._instant("plan", "plan", output=clip(data.get("plan")))

    def _on_tool_call(self, data: dict[str, Any]) -> None:
        name = str(data.get("name"))
        key = str(data.get("id") or f"_anon{next(self._ids)}")
        if not data.get("id"):
            self._tool_fifo.setdefault(name, deque()).append(key)
        md = {"tool": name}
        if data.get("turn") is not None:
            md["turn"] = data["turn"]
        self._tools[key] = self._open(
            "tool_call",
            f"tool:{name}",
            self._clock(),
            self._container(),
            input=clip(data.get("input")),
            metadata=md,
        )

    def _on_tool_result(self, data: dict[str, Any]) -> None:
        name = str(data.get("name"))
        key = data.get("id")
        span = self._tools.pop(str(key), None) if key else None
        if span is None:
            fifo = self._tool_fifo.get(name)
            while fifo and span is None:
                span = self._tools.pop(fifo.popleft(), None)
        now = self._clock()
        if span is None:
            span = self._open("tool_call", f"tool:{name}", now, self._container())
        ok = bool(data.get("ok", not data.get("is_error", False)))
        span.output = clip(data.get("summary"))
        message = None if ok else str(data.get("error") or data.get("summary") or "tool error")
        self._close(span, now, "ok" if ok else "error", message)

    def _on_approval_required(self, data: dict[str, Any]) -> None:
        tool = str(data.get("tool"))
        parent = next(
            (s for s in reversed(list(self._tools.values())) if s.metadata.get("tool") == tool),
            None,
        )
        md = {
            k: data.get(k)
            for k in ("tool", "reasons", "triggers", "estimated_rows", "timeout_s")
            if data.get(k) is not None
        }
        span = self._open(
            "approval",
            f"approval:{tool}",
            self._clock(),
            parent or self._container(),
            input=clip(data.get("summary")),
            metadata=md,
        )
        self._approvals[str(data.get("id"))] = span

    def _on_approval_decision(self, data: dict[str, Any]) -> None:
        span = self._approvals.pop(str(data.get("action_id")), None)
        now = self._clock()
        if span is None:
            span = self._open("approval", f"approval:{data.get('tool')}", now, self._container())
        approved = bool(data.get("approved"))
        span.metadata.update(
            {
                "approved": approved,
                "decided_by": data.get("decided_by"),
                "waited_s": data.get("waited_s"),
            }
        )
        span.output = {"approved": approved, "note": data.get("note")}
        self._close(span, now, "ok" if approved else "warning", None if approved else "denied")

    def _on_critic(self, data: dict[str, Any]) -> None:
        now = self._clock()
        span = self._critic or self._open("critic", "critic", now, self._container())
        self._critic = None
        verdict = data.get("verdict")
        span.metadata.update({"verdict": verdict, "round": data.get("round")})
        span.output = {k: v for k, v in data.items() if k not in ("round",)}
        revise = verdict == "revise"
        self._close(span, now, "warning" if revise else "ok", "revise" if revise else None)

    def _on_route(self, data: dict[str, Any]) -> None:
        now = self._clock()
        span = self._route or self._open("route", "route", now, self.root)
        self._route = None
        md = {
            k: data.get(k)
            for k in ("tier", "model", "reason", "router_model", "source", "error")
            if data.get(k) is not None
        }
        span.metadata.update(md)
        span.output = md
        self._tier = data.get("tier")
        self.root.metadata["routed_tier"] = self._tier
        self.root.metadata["routed_model"] = data.get("model")
        fallback = data.get("source") == "fallback"
        self._close(span, now, "warning" if fallback else "ok", data.get("error"))

    def _on_escalation(self, data: dict[str, Any]) -> None:
        self._tier = data.get("to_tier") or self._tier
        self.root.metadata["final_tier"] = self._tier
        self._instant(
            "escalation",
            f"escalation:{data.get('from_tier')}->{data.get('to_tier')}",
            status="warning",
            metadata={
                k: data.get(k)
                for k in ("from_tier", "to_tier", "from_model", "to_model", "trigger", "detail")
            },
        )

    def _on_semantic_retrieval(self, data: dict[str, Any]) -> None:
        self._instant(
            "retrieval",
            "semantic_retrieval",
            parent=self.root,
            output={"metrics": data.get("metrics"), "scores": data.get("scores")},
            status="error" if data.get("error") else "ok",
            message=data.get("error"),
        )

    def _on_stop(self, data: dict[str, Any]) -> None:
        self.root.metadata["agent_status"] = data.get("status")
        self.root.metadata["detail"] = data.get("detail")
        self._close_turn(self._clock())

    # ---- helpers -----------------------------------------------------------------------

    def _container(self) -> Span:
        return self._turn or self.root

    def _open_turn(self, start: float, data: dict[str, Any]) -> None:
        self._turn_count += 1
        n = int(data.get("turn") or self._turn_count)
        self._turn_count = n
        md: dict[str, Any] = {"turn": n}
        if data.get("model"):
            md["model"] = data["model"]
        tier = data.get("tier") or self._tier
        if tier:
            md["tier"] = tier
        self._turn = self._open("turn", f"turn {n}", start, self.root, metadata=md)

    def _close_turn(self, now: float) -> None:
        if self._turn is not None:
            turn, self._turn = self._turn, None
            turn.metadata["cost_usd"] = round(turn.total_cost(), 6)
            self._close(turn, now)

    def _open(
        self,
        kind: str,
        name: str,
        start: float,
        parent: Span,
        *,
        input: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> Span:
        span = Span(
            id=f"s{next(self._ids)}",
            kind=kind,
            name=name,
            start=start,
            input=input,
            metadata=dict(metadata or {}),
        )
        parent.children.append(span)
        self._notify("span_started", span, parent)
        return span

    def _close(
        self, span: Span, end: float, status: str = "ok", message: str | None = None
    ) -> None:
        span.end = end
        span.status = status if span.status == "ok" else span.status
        span.status_message = message if message is not None else span.status_message
        if span.kind in ("route", "critic") and "cost_usd" not in span.metadata:
            span.metadata["cost_usd"] = round(span.total_cost(), 6)
        self._notify("span_ended", span)

    def _instant(
        self,
        kind: str,
        name: str,
        *,
        parent: Span | None = None,
        output: Any = None,
        status: str = "ok",
        message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = self._clock()
        span = self._open(kind, name, now, parent or self._container(), metadata=metadata)
        span.output = output
        self._close(span, now, status, message)

    def _totals(self) -> dict[str, Any]:
        llm = [s for s in self.root.walk() if s.kind == "llm_call"]
        models = sorted({str(s.metadata["model"]) for s in llm if s.metadata.get("model")})
        return {
            "cost_usd": round(sum(s.own_cost for s in llm), 6),
            "llm_calls": len(llm),
            "input_tokens": sum(int(s.metadata.get("input_tokens") or 0) for s in llm),
            "output_tokens": sum(int(s.metadata.get("output_tokens") or 0) for s in llm),
            "models": models,
            "turns": sum(1 for s in self.root.children if s.kind == "turn"),
            "tool_calls": sum(1 for s in self.root.walk() if s.kind == "tool_call"),
        }

    def _notify(self, method: str, *args: Any) -> None:
        for sink in self._sinks:
            self._call(sink, method, *args)

    @staticmethod
    def _call(sink: SpanSink, method: str, *args: Any) -> None:
        try:
            getattr(sink, method)(*args)
        except Exception:  # noqa: BLE001 - a broken sink must not break the run
            log.exception("trace sink %s.%s failed", type(sink).__name__, method)


# ---- LLM observer --------------------------------------------------------------------------

EmitFn = Callable[[str, dict[str, Any]], None]


class EventedLLM:
    """Wraps a ``core.llm.LLM`` and emits one ``llm_call`` event per completed request.

    All calls still go through the wrapped client (``core/llm.py``); this only observes them.
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


# ---- persistence ---------------------------------------------------------------------------


def trace_document(root: Span, **extra: Any) -> dict[str, Any]:
    md = root.metadata
    doc = {
        "trace_format": TRACE_FORMAT_VERSION,
        "run_id": md.get("run_id"),
        "job_id": md.get("job_id"),
        "task_id": md.get("task_id"),
        "trace_id": md.get("langfuse_trace_id"),
        "spans": root.to_dict(),
    }
    doc.update(extra)
    return doc


def save_trace(directory: Path, root: Span, name: str | None = None) -> Path:
    """Write ``<directory>/<name or run_id>.json`` and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    stem = name or root.metadata.get("run_id") or root.metadata.get("job_id") or new_run_id()
    path = directory / f"{stem}.json"
    path.write_text(json.dumps(trace_document(root), indent=2, default=str))
    return path


def load_trace(path: Path) -> Span:
    """Root span from a saved trace: a :func:`trace_document`, or any JSON with ``spans``
    (``ask --trace``), or a bare span dict."""
    data = json.loads(path.read_text())
    if isinstance(data, dict) and isinstance(data.get("spans"), dict):
        return Span.from_dict(data["spans"])
    if isinstance(data, dict) and "kind" in data and "children" in data:
        return Span.from_dict(data)
    raise ValueError(f"{path} does not contain a span tree (no 'spans' key)")


def find_trace(ref: str, search_dirs: list[Path]) -> Path | None:
    """Resolve a run id, job id, task id or file path to a saved trace file."""
    p = Path(ref)
    if p.is_file():
        return p
    for d in search_dirs:
        direct = d / f"{ref}.json"
        if direct.is_file():
            return direct
    for d in search_dirs:
        if not d.is_dir():
            continue
        for candidate in sorted(d.rglob("*.json")):
            try:
                doc = json.loads(candidate.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(doc, dict) or not isinstance(doc.get("spans"), dict):
                continue
            md = doc["spans"].get("metadata") or {}
            if ref in {doc.get(k) for k in ("run_id", "job_id", "task_id")} | {
                md.get(k) for k in ("run_id", "job_id", "task_id")
            }:
                return candidate
    return None


# ---- rendering -----------------------------------------------------------------------------

_STATUS_MARK = {"ok": "✓", "warning": "!", "error": "✗"}


def _fmt_s(seconds: float | None) -> str:
    if seconds is None:
        return "open"
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    return f"{seconds:.2f}s"


def _label(span: Span) -> str:
    md = span.metadata
    if span.kind == "run":
        parts = [f"run {md.get('run_id') or ''}".strip()]
        for key in ("job_id", "task_id"):
            if md.get(key):
                parts.append(f"{key}={md[key]}")
        if md.get("status"):
            parts.append(f"status={md['status']}")
        if md.get("routed_tier"):
            tier = md["routed_tier"]
            if md.get("final_tier") and md["final_tier"] != tier:
                tier += f"→{md['final_tier']}"
            parts.append(f"tier={tier}")
        return " ".join(parts)
    if span.kind == "llm_call":
        bits = [span.name, str(md.get("model") or "?")]
        if md.get("tier"):
            bits.append(f"[{md['tier']}]")
        bits.append(f"in={md.get('input_tokens', 0):,} out={md.get('output_tokens', 0):,}")
        if md.get("cache_read_tokens"):
            bits.append(f"cache_read={md['cache_read_tokens']:,}")
        if md.get("prompt_version"):
            bits.append(f"prompt={md.get('prompt')}@{md['prompt_version']}")
        return " ".join(bits)
    if span.kind == "turn":
        extra = " ".join(f"{k}={md[k]}" for k in ("model", "tier") if md.get(k))
        return f"{span.name} {extra}".strip()
    if span.kind == "route":
        return f"route {md.get('tier', '?')} → {md.get('model', '?')}" + (
            f" ({md['source']})" if md.get("source") and md["source"] != "router" else ""
        )
    if span.kind == "critic":
        return f"critic round={md.get('round')} verdict={md.get('verdict')}"
    if span.kind == "approval":
        if "approved" in md:
            verdict = "approved" if md["approved"] else "denied"
            return f"{span.name} {verdict} by {md.get('decided_by')}"
        return f"{span.name} pending"
    if span.kind == "plan":
        text = str(span.output or "").strip().splitlines()
        first = text[0] if text else ""
        return f'plan "{first[:60]}{"…" if len(first) > 60 or len(text) > 1 else ""}"'
    if span.kind == "escalation":
        return f"{span.name} ({md.get('trigger')})"
    return span.name


def render_tree(root: Span, *, show_instant: bool = True, color: bool = False) -> str:
    """Pretty-print a span tree: offset from run start, duration, cost, status per line."""
    t0 = root.start
    lines: list[str] = []

    def paint(text: str, status: str) -> str:
        if not color or status == "ok":
            return text
        code = "33" if status == "warning" else "31"
        return f"\x1b[{code}m{text}\x1b[0m"

    rows: list[tuple[str, Span]] = []

    def visit(span: Span, prefix: str, child_prefix: str) -> None:
        head = f"{prefix}{_STATUS_MARK.get(span.status, '?')} {_label(span)}"
        rows.append((head, span))
        kids = [c for c in span.children if show_instant or c.duration]
        for i, c in enumerate(kids):
            last = i == len(kids) - 1
            visit(
                c,
                child_prefix + ("└─ " if last else "├─ "),
                child_prefix + ("   " if last else "│  "),
            )

    visit(root, "", "")
    width = min(max(len(h) for h, _ in rows), 100)
    for head, span in rows:
        cost = span.total_cost()
        cost_s = f"${cost:.4f}" if cost else ""
        timing = f"+{span.start - t0:7.2f}s {_fmt_s(span.duration):>8}"
        text = f"{head:<{width}}  {timing} {cost_s:>9}".rstrip()
        if span.status != "ok" and span.status_message:
            text += f"  ({span.status_message[:80]})"
        lines.append(paint(text, span.status))
    md = root.metadata
    lines.append("")
    summary = [
        f"total {_fmt_s(root.duration)}",
        f"${root.total_cost():.4f}",
        f"{md.get('llm_calls', sum(1 for s in root.walk() if s.kind == 'llm_call'))} LLM calls",
        f"{md.get('tool_calls', sum(1 for s in root.walk() if s.kind == 'tool_call'))} tool calls",
    ]
    if md.get("models"):
        summary.append("models: " + ", ".join(md["models"]))
    lines.append(" | ".join(summary))
    if md.get("prompt_versions"):
        lines.append(
            "prompts: " + ", ".join(f"{k}@{v}" for k, v in sorted(md["prompt_versions"].items()))
        )
    return "\n".join(lines)
