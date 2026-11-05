"""Build a :class:`ScoringBackend` by name (``AGENT_SEMANTIC_BACKEND``)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, get_args

from analyst_agent.semantic.embeddings import (
    DEFAULT_EMBEDDING_MODEL,
    EmbeddingBackend,
    Encoder,
    HybridBackend,
)
from analyst_agent.semantic.retriever import BM25Backend, ScoringBackend
from analyst_agent.semantic.vocabulary import Vocabulary

BackendName = Literal["bm25", "embedding", "hybrid"]
BACKENDS: tuple[str, ...] = get_args(BackendName)


def make_backend(
    name: str = "bm25",
    *,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    cache_dir: Path | str | None = None,
    hybrid_weight: float = 0.5,
    encoder: Encoder | None = None,
    vocabulary: Vocabulary | None = None,
) -> ScoringBackend:
    """``bm25`` | ``embedding`` | ``hybrid``. ``hybrid_weight`` is the embedding share.

    ``vocabulary`` (analyst phrase table) expands BM25 queries, in ``bm25`` and in the sparse
    half of ``hybrid``; embeddings see the question as asked. Nothing is downloaded or imported
    here: the embedding model loads on first use.
    """
    if name == "bm25":
        return BM25Backend(vocabulary=vocabulary)
    if name not in BACKENDS:
        raise ValueError(f"unknown semantic backend {name!r}; expected one of {BACKENDS}")
    dense = EmbeddingBackend(embedding_model, cache_dir=cache_dir, encoder=encoder)
    if name == "embedding":
        return dense
    return HybridBackend(BM25Backend(vocabulary=vocabulary), dense, weight=hybrid_weight)
