"""Semantic layer: governed metric definitions retrieved into the planner prompt.

Usage::

    from analyst_agent.semantic import MetricRetriever, load_metrics

    retriever = MetricRetriever(load_metrics("benchmarks/metrics"))
    hits = retriever.retrieve("What was churn in 2025?", k=3, tables=["subscriptions"])

Enabled in the agent with ``AGENT_SEMANTIC_LAYER_ENABLED=true`` (see ``core.config``).
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

from analyst_agent.semantic.loader import MetricsLoadError, load_metrics, load_metrics_file
from analyst_agent.semantic.retriever import (
    BM25Backend,
    MetricRetriever,
    RetrievedMetric,
    ScoringBackend,
    tokenize,
)
from analyst_agent.semantic.schema import Metric, MetricsFile

__all__ = [
    "BM25Backend",
    "Metric",
    "MetricRetriever",
    "MetricsFile",
    "MetricsLoadError",
    "RetrievedMetric",
    "ScoringBackend",
    "default_retriever",
    "load_metrics",
    "load_metrics_file",
    "tokenize",
]


@lru_cache(maxsize=8)
def _cached_retriever(paths: tuple[Path, ...]) -> MetricRetriever:
    return MetricRetriever(load_metrics(paths))


def default_retriever(paths: Path | str | Sequence[Path | str]) -> MetricRetriever:
    """BM25 retriever over the metric files at ``paths``, cached per path set."""
    if isinstance(paths, str | Path):
        paths = [paths]
    return _cached_retriever(tuple(Path(p).resolve() for p in paths))
