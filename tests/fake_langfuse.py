"""In-memory stand-in for the Langfuse SDK client (the subset we call). Never hits the network."""

from __future__ import annotations

import uuid
from typing import Any


class FakeObservation:
    def __init__(self, client: FakeLangfuse, trace_id: str, **kwargs: Any) -> None:
        self.client = client
        self.trace_id = trace_id
        self.id = uuid.uuid4().hex[:16]
        self.kwargs = kwargs
        self.updates: list[dict[str, Any]] = []
        self.ended = False
        self.children: list[FakeObservation] = []
        client.log.append(self)

    @property
    def name(self) -> str:
        return self.kwargs["name"]

    @property
    def as_type(self) -> str:
        return self.kwargs.get("as_type", "span")

    @property
    def metadata(self) -> dict[str, Any]:
        """Metadata as Langfuse would hold it after all updates."""
        md = dict(self.kwargs.get("metadata") or {})
        for u in self.updates:
            md.update(u.get("metadata") or {})
        return md

    @property
    def level(self) -> str | None:
        return next((u["level"] for u in reversed(self.updates) if "level" in u), None)

    def start_observation(self, **kwargs: Any) -> FakeObservation:
        child = FakeObservation(self.client, self.trace_id, **kwargs)
        self.children.append(child)
        return child

    def update(self, **kwargs: Any) -> FakeObservation:
        self.updates.append(kwargs)
        return self

    def end(self, **_: Any) -> FakeObservation:
        self.ended = True
        return self

    def find(self, name: str) -> list[FakeObservation]:
        out = [self] if self.kwargs.get("name") == name else []
        for c in self.children:
            out += c.find(name)
        return out


class FakeLangfuse:
    def __init__(self) -> None:
        self.log: list[FakeObservation] = []
        self.roots: list[FakeObservation] = []
        self.scores: list[dict[str, Any]] = []
        self.flushed = 0
        self.closed = False

    def start_observation(
        self, *, trace_context: dict[str, Any] | None = None, **kwargs: Any
    ) -> FakeObservation:
        trace_id = (trace_context or {}).get("trace_id") or uuid.uuid4().hex
        root = FakeObservation(self, trace_id, **kwargs)
        self.roots.append(root)
        return root

    def create_score(self, **kwargs: Any) -> None:
        self.scores.append(kwargs)

    def scores_for(self, trace_id: str) -> dict[str, Any]:
        return {s["name"]: s["value"] for s in self.scores if s["trace_id"] == trace_id}

    def flush(self) -> None:
        self.flushed += 1

    def shutdown(self) -> None:
        self.closed = True
