"""Langfuse export, behind ``AGENT_LANGFUSE_ENABLED``.

* :class:`LangfuseSink` mirrors a :class:`~analyst_agent.core.tracing.TraceRecorder` tree into
  Langfuse observations as it is built (one Langfuse trace per run):

  ======================  ==============
  span kind               Langfuse type
  ======================  ==============
  run                     ``agent``
  turn / plan / …         ``span``
  llm_call                ``generation`` (model, usage, cost, ``version`` = prompt version)
  tool_call               ``tool``
  critic                  ``evaluator``
  approval                ``guardrail``
  route                   ``chain``
  retrieval               ``retriever``
  ======================  ==============

  Cost, model, prompt version and routed tier are attached as metadata on every observation
  that has them, so Langfuse can filter / group on them.

* :func:`push_scores` attaches eval results to a run's trace as Langfuse scores (``accuracy``,
  ``passed``, ``judge_score``, ``cost_usd``, ``eval_status``) so traces can be filtered by
  pass/fail.

The Langfuse SDK is an optional dependency (``api`` extra) and is imported lazily. Nothing in
here may fail a run: SDK errors are logged by the recorder (sink calls) or here (scores).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from analyst_agent.core.tracing import Span, clip

log = logging.getLogger(__name__)

AS_TYPE = {
    "run": "agent",
    "llm_call": "generation",
    "tool_call": "tool",
    "critic": "evaluator",
    "approval": "guardrail",
    "route": "chain",
    "retrieval": "retriever",
}
LEVEL = {"ok": "DEFAULT", "warning": "WARNING", "error": "ERROR"}


def new_trace_id() -> str:
    """A Langfuse / W3C trace id (32 lowercase hex chars)."""
    return uuid.uuid4().hex


def make_langfuse_client() -> Any | None:
    """``langfuse.Langfuse()`` from the standard ``LANGFUSE_*`` env vars, or None on failure."""
    try:
        from langfuse import Langfuse  # optional dependency

        return Langfuse()
    except Exception:  # noqa: BLE001
        log.exception("AGENT_LANGFUSE_ENABLED is set but Langfuse could not be initialised")
        return None


def _usage(md: dict[str, Any]) -> dict[str, int] | None:
    usage = {
        dst: int(md[src])
        for dst, src in (
            ("input", "input_tokens"),
            ("output", "output_tokens"),
            ("cache_read_input_tokens", "cache_read_tokens"),
            ("cache_creation_input_tokens", "cache_write_tokens"),
        )
        if md.get(src)
    }
    return usage or None


class LangfuseSink:
    """Live mirror of one run's span tree into one Langfuse trace."""

    def __init__(
        self, client: Any, *, trace_id: str | None = None, root_name: str = "analyst-run"
    ) -> None:
        self._client = client
        self._trace_id = trace_id
        self._root_name = root_name
        self._obs: dict[str, Any] = {}

    @property
    def trace_id(self) -> str | None:
        return self._trace_id

    def span_started(self, span: Span, parent: Span | None) -> None:
        kwargs: dict[str, Any] = {
            "as_type": AS_TYPE.get(span.kind, "span"),
            "input": clip(span.input),
            "metadata": dict(span.metadata) or None,
        }
        md = span.metadata
        if span.kind == "llm_call":
            kwargs.update(
                model=md.get("model"),
                usage_details=_usage(md),
                cost_details={"total": float(md.get("cost_usd") or 0.0)},
                version=md.get("prompt_version"),
            )
        if parent is None:
            ctx = {"trace_id": self._trace_id} if self._trace_id else None
            obs = self._client.start_observation(trace_context=ctx, name=self._root_name, **kwargs)
            self._trace_id = getattr(obs, "trace_id", None) or self._trace_id
            span.metadata["langfuse_trace_id"] = self._trace_id
        else:
            parent_obs = self._obs.get(parent.id)
            if parent_obs is None:
                return  # parent never made it to Langfuse (e.g. SDK error); drop the subtree
            obs = parent_obs.start_observation(name=span.name, **kwargs)
        self._obs[span.id] = obs

    def span_ended(self, span: Span) -> None:
        obs = self._obs.pop(span.id, None)
        if obs is None:
            return
        update: dict[str, Any] = {
            "output": clip(span.output),
            "metadata": {
                **span.metadata,
                "cost_usd": round(span.total_cost(), 6),
                "duration_s": None if span.duration is None else round(span.duration, 4),
            },
            "level": LEVEL.get(span.status, "DEFAULT"),
        }
        if span.status_message:
            update["status_message"] = span.status_message
        obs.update(**update)
        obs.end()

    def close(self, root: Span) -> None:
        self._client.flush()


# ---- eval scores ---------------------------------------------------------------------------


def eval_scores(
    *,
    status: str,
    passed: bool,
    score: float,
    score_details: dict[str, Any],
    cost_usd: float,
) -> list[dict[str, Any]]:
    """The Langfuse scores for one eval task (``create_score`` kwargs without ``trace_id``)."""
    method = score_details.get("method")
    skipped = bool(score_details.get("skipped"))
    out: list[dict[str, Any]] = [
        {"name": "eval_status", "value": status, "data_type": "CATEGORICAL"},
        {"name": "cost_usd", "value": float(cost_usd), "data_type": "NUMERIC"},
    ]
    if status == "ok" and method not in ("unscored", "not_run") and not skipped:
        out += [
            {"name": "accuracy", "value": float(score), "data_type": "NUMERIC"},
            {"name": "passed", "value": 1.0 if passed else 0.0, "data_type": "BOOLEAN"},
        ]
    elif status != "ok":  # timeouts / crashes count as failures in pass/fail filters
        out.append({"name": "passed", "value": 0.0, "data_type": "BOOLEAN"})
    judge = judge_score(score_details)
    if judge is not None:
        out.append({"name": "judge_score", "value": judge, "data_type": "NUMERIC"})
    return out


def judge_score(details: dict[str, Any]) -> float | None:
    """The LLM judge's part of a score: rubric fraction (L3) or behaviour pass (ADV)."""
    method = details.get("method")
    if method == "rubric_judge":
        if details.get("skipped") or details.get("error"):
            return None
        points = details.get("points") or []
        return round(sum(p.get("score", 0) for p in points) / len(points), 4) if points else None
    judge = details.get("judge")
    if method == "adversarial" and isinstance(judge, dict) and not judge.get("skipped"):
        if judge.get("error"):
            return None
        return 1.0 if judge.get("passed") else 0.0
    return None


def push_scores(
    client: Any,
    trace_id: str,
    scores: list[dict[str, Any]],
    *,
    comment: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    """``client.create_score`` for each score; returns how many were sent. Never raises."""
    sent = 0
    for s in scores:
        try:
            client.create_score(trace_id=trace_id, comment=comment, metadata=metadata, **s)
            sent += 1
        except Exception:  # noqa: BLE001
            log.exception("could not push Langfuse score %s for trace %s", s.get("name"), trace_id)
    return sent
