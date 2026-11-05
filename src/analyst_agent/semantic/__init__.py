"""Semantic layer: governed metric definitions retrieved into the planner prompt.

Usage::

    from analyst_agent.semantic import MetricRetriever, load_metrics

    retriever = MetricRetriever(load_metrics("benchmarks/metrics"))
    hits = retriever.retrieve("What was churn in 2025?", k=3, tables=["subscriptions"])

Enabled in the agent with ``AGENT_SEMANTIC_LAYER_ENABLED=true`` (see ``core.config``). The
scoring backend is ``AGENT_SEMANTIC_BACKEND=bm25|embedding|hybrid``; the embedding backends need
the optional ``embeddings`` extra. ``python -m analyst_agent.semantic eval`` measures retrieval
quality per backend against ``benchmarks/metrics/retrieval_eval.yaml``.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

from analyst_agent.semantic.backends import BACKENDS, make_backend
from analyst_agent.semantic.embeddings import (
    DEFAULT_EMBEDDING_MODEL,
    EmbeddingBackend,
    HybridBackend,
    VectorCache,
)
from analyst_agent.semantic.loader import MetricsLoadError, load_metrics, load_metrics_file
from analyst_agent.semantic.retriever import (
    BM25Backend,
    MetricRetriever,
    RetrievedMetric,
    ScoringBackend,
    tokenize,
)
from analyst_agent.semantic.schema import Metric, MetricsFile
from analyst_agent.semantic.vocabulary import Vocabulary, load_vocabulary

__all__ = [
    "BACKENDS",
    "BM25Backend",
    "DEFAULT_EMBEDDING_MODEL",
    "EmbeddingBackend",
    "HybridBackend",
    "Metric",
    "MetricRetriever",
    "MetricsFile",
    "MetricsLoadError",
    "RetrievedMetric",
    "ScoringBackend",
    "default_retriever",
    "load_metrics",
    "load_metrics_file",
    "VectorCache",
    "Vocabulary",
    "load_vocabulary",
    "make_backend",
    "tokenize",
]


@lru_cache(maxsize=8)
def _cached_retriever(
    paths: tuple[Path, ...],
    backend: str,
    embedding_model: str,
    cache_dir: Path | None,
    hybrid_weight: float,
) -> MetricRetriever:
    return MetricRetriever(
        load_metrics(paths),
        make_backend(
            backend,
            embedding_model=embedding_model,
            cache_dir=cache_dir,
            hybrid_weight=hybrid_weight,
            vocabulary=load_vocabulary(paths),
        ),
    )


def default_retriever(
    paths: Path | str | Sequence[Path | str],
    *,
    backend: str = "bm25",
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    cache_dir: Path | str | None = None,
    hybrid_weight: float = 0.5,
) -> MetricRetriever:
    """Retriever over the metric files at ``paths``, cached per path set and backend config.

    Any ``kind: vocabulary`` file at ``paths`` (see :mod:`.vocabulary`) expands BM25 queries.
    """
    if isinstance(paths, str | Path):
        paths = [paths]
    return _cached_retriever(
        tuple(Path(p).resolve() for p in paths),
        backend,
        embedding_model,
        Path(cache_dir).resolve() if cache_dir is not None else None,
        hybrid_weight,
    )
