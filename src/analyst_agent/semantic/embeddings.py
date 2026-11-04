"""Dense and hybrid :class:`~analyst_agent.semantic.retriever.ScoringBackend` implementations.

- :class:`EmbeddingBackend` scores by cosine similarity of sentence embeddings (default model
  ``sentence-transformers/all-MiniLM-L6-v2``). ``sentence-transformers`` is an optional extra
  (``uv sync --extra embeddings``) and is imported only when a vector actually has to be computed;
  document vectors are cached on disk keyed by model and text, so a warm start never loads the
  model just to fit.
- :class:`HybridBackend` combines a sparse (BM25) and a dense backend: each is max-normalised to
  ``[0, 1]`` per query, then mixed as ``(1 - weight) * sparse + weight * dense``.

Select either by name with :func:`analyst_agent.semantic.make_backend` /
``AGENT_SEMANTIC_BACKEND``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from analyst_agent.semantic.retriever import ScoringBackend

log = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
INSTALL_HINT = (
    "the embedding backend needs the optional 'embeddings' extra: "
    "uv sync --extra embeddings  (or pip install 'analyst-agent[embeddings]')"
)


class Encoder(Protocol):
    """The slice of ``SentenceTransformer`` we use: texts in, an ``(n, dim)`` array out."""

    def encode(self, sentences: list[str], **kwargs: Any) -> Any: ...


def load_sentence_transformer(model_name: str) -> Encoder:
    """Import ``sentence_transformers`` and load ``model_name`` (downloads on first use)."""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as e:  # pragma: no cover - exercised only without the extra
        raise ImportError(INSTALL_HINT) from e
    log.info("loading sentence-transformers model %s", model_name)
    return SentenceTransformer(model_name)


def _normalize(vectors: np.ndarray) -> np.ndarray:
    vectors = np.asarray(vectors, dtype=np.float32)
    if vectors.ndim == 1:
        vectors = vectors[None, :]
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.where(norms == 0, 1.0, norms)


class VectorCache:
    """On-disk cache of unit-normalised vectors: one ``.npz`` per model, keyed by text hash.

    Writes are atomic (temp file + rename), so concurrent processes at worst recompute a vector;
    they never read a torn file. A corrupt or unreadable cache file is ignored and rewritten.
    """

    def __init__(self, cache_dir: Path | str, model_name: str) -> None:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "__", model_name).strip("_") or "model"
        self.path = Path(cache_dir) / f"{slug}.npz"
        self.model_name = model_name

    def key(self, text: str) -> str:
        return "v" + hashlib.sha256(f"{self.model_name}\0{text}".encode()).hexdigest()

    def load(self) -> dict[str, np.ndarray]:
        if not self.path.exists():
            return {}
        try:
            with np.load(self.path, allow_pickle=False) as z:
                return {k: z[k] for k in z.files}
        except Exception as e:  # noqa: BLE001 - a bad cache must never break retrieval
            log.warning("ignoring unreadable embedding cache %s: %s", self.path, e)
            return {}

    def save(self, vectors: dict[str, np.ndarray]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".tmp-", suffix=".npz")
        try:
            with os.fdopen(fd, "wb") as f:
                np.savez(f, **vectors)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


class EmbeddingBackend:
    """Cosine similarity between the question and each document embedding.

    ``encoder`` injects a ready encoder (tests pass a fake); otherwise ``encoder_factory``
    (default: :func:`load_sentence_transformer`) is called lazily, the first time a vector is
    not in the cache. Similarities below ``min_similarity`` are reported as 0 (no match), so
    unrelated metrics are not returned just because every cosine is slightly positive.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_EMBEDDING_MODEL,
        *,
        cache_dir: Path | str | None = None,
        encoder: Encoder | None = None,
        encoder_factory: Callable[[str], Encoder] | None = None,
        min_similarity: float = 0.2,
    ) -> None:
        self.model_name = model_name
        self.cache = VectorCache(cache_dir, model_name) if cache_dir is not None else None
        self._encoder = encoder
        self._encoder_factory = encoder_factory or load_sentence_transformer
        self.min_similarity = min_similarity
        self._doc_vectors: np.ndarray | None = None
        self._n = 0

    @property
    def encoder(self) -> Encoder:
        if self._encoder is None:
            self._encoder = self._encoder_factory(self.model_name)
        return self._encoder

    @property
    def encoder_loaded(self) -> bool:
        return self._encoder is not None

    def _encode(self, texts: list[str]) -> np.ndarray:
        return _normalize(self.encoder.encode(texts))

    def fit(self, documents: Sequence[str]) -> None:
        docs = list(documents)
        self._n = len(docs)
        if not docs:
            self._doc_vectors = None
            return
        cached = self.cache.load() if self.cache else {}
        keys = (
            [self.cache.key(d) for d in docs] if self.cache else [str(i) for i in range(len(docs))]
        )
        missing = [i for i, k in enumerate(keys) if k not in cached]
        if missing:
            fresh = self._encode([docs[i] for i in missing])
            for i, vec in zip(missing, fresh, strict=True):
                cached[keys[i]] = vec
            if self.cache:
                self.cache.save(cached)
        self._doc_vectors = _normalize(np.stack([cached[k] for k in keys]))

    def score(self, query: str) -> list[float]:
        if self._doc_vectors is None or not query.strip():
            return [0.0] * self._n
        q = self._encode([query])[0]
        sims = self._doc_vectors @ q
        return [float(s) if s >= self.min_similarity else 0.0 for s in sims]


def _max_normalize(scores: Sequence[float]) -> list[float]:
    top = max(scores, default=0.0)
    if top <= 0:
        return [0.0] * len(scores)
    return [max(0.0, s) / top for s in scores]


class HybridBackend:
    """Weighted mix of a sparse and a dense backend, each max-normalised per query.

    ``weight`` is the dense share: 0 is pure BM25 ranking, 1 pure embedding ranking. A document
    scores 0 only if both backends give it 0, so the retriever's ``score > min_score`` filter
    keeps its meaning.
    """

    def __init__(self, sparse: ScoringBackend, dense: ScoringBackend, weight: float = 0.5) -> None:
        if not 0.0 <= weight <= 1.0:
            raise ValueError(f"hybrid weight must be in [0, 1], got {weight}")
        self.sparse = sparse
        self.dense = dense
        self.weight = weight

    def fit(self, documents: Sequence[str]) -> None:
        self.sparse.fit(documents)
        self.dense.fit(documents)

    def score(self, query: str) -> list[float]:
        sparse = _max_normalize(self.sparse.score(query))
        # Skip the dense model entirely when it has no say.
        dense = _max_normalize(self.dense.score(query)) if self.weight > 0 else [0.0] * len(sparse)
        w = self.weight
        return [(1 - w) * s + w * d for s, d in zip(sparse, dense, strict=True)]
