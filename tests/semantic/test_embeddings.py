"""Embedding and hybrid backends with a fake encoder: nothing is downloaded or imported."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from analyst_agent.semantic import (
    BM25Backend,
    EmbeddingBackend,
    HybridBackend,
    MetricRetriever,
    ScoringBackend,
    VectorCache,
    default_retriever,
    make_backend,
    tokenize,
)
from analyst_agent.semantic.backends import BACKENDS
from tests.semantic.test_retriever import METRICS

# Words the fake model treats as synonyms: each group maps to one dimension.
CONCEPTS = [
    {"churn", "cancel", "leave", "lost", "subscription"},
    {"revenue", "mrr", "money", "recurring"},
    {"return", "refund", "returned", "order"},
    {"employee", "staff", "turnover", "terminations", "headcount", "people"},
    {"global", "note", "dependency"},
]


class FakeEncoder:
    """Bag-of-concepts 'embedding': deterministic, offline, and semantically meaningful enough to
    rank paraphrases with no word overlap (``people quitting`` -> attrition)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def encode(self, sentences: list[str], **kwargs: Any) -> np.ndarray:
        self.calls.append(list(sentences))
        out = np.zeros((len(sentences), len(CONCEPTS) + 1), dtype=np.float32)
        for row, s in enumerate(sentences):
            for t in tokenize(s.replace("quitting", "leave people")):
                for d, words in enumerate(CONCEPTS):
                    if t in words:
                        out[row, d] += 1
            out[row, -1] = 0.01  # never an all-zero vector
        return out


def no_download(model_name: str) -> Any:
    raise AssertionError(f"tests must not load a real model ({model_name})")


def dense(**kw: Any) -> tuple[EmbeddingBackend, FakeEncoder]:
    enc = FakeEncoder()
    return EmbeddingBackend("fake/model", encoder=enc, encoder_factory=no_download, **kw), enc


def test_embedding_backend_ranks_paraphrases() -> None:
    backend, _ = dense()
    assert isinstance(backend, ScoringBackend)
    r = MetricRetriever(METRICS, backend=backend)
    assert r.retrieve("people quitting", k=1)[0].metric.name == "attrition_rate"
    # BM25 has no shared token, so it finds nothing.
    assert MetricRetriever(METRICS).retrieve("people quitting", k=1) == []


def test_min_similarity_zeroes_weak_matches() -> None:
    backend, _ = dense(min_similarity=0.5)
    backend.fit([m.search_text() for m in METRICS])
    scores = backend.score("staff turnover")
    assert scores[3] > 0.5
    assert scores[0] == 0.0
    assert backend.score("   ") == [0.0] * len(METRICS)


def test_encoder_is_lazy_and_cached_on_disk(tmp_path: Path) -> None:
    docs = [m.search_text() for m in METRICS]
    factory_calls: list[str] = []
    enc = FakeEncoder()

    def factory(name: str) -> FakeEncoder:
        factory_calls.append(name)
        return enc

    b1 = EmbeddingBackend("fake/model", cache_dir=tmp_path, encoder_factory=factory)
    assert not b1.encoder_loaded  # constructing never loads the model
    b1.fit(docs)
    assert factory_calls == ["fake/model"] and enc.calls == [docs]
    cache_files = list(tmp_path.glob("*.npz"))
    assert [p.name for p in cache_files] == ["fake__model.npz"]

    # Warm start: fitting the same corpus reads the cache and never loads the model.
    b2 = EmbeddingBackend("fake/model", cache_dir=tmp_path, encoder_factory=no_download)
    b2.fit(docs)
    assert not b2.encoder_loaded
    np.testing.assert_allclose(b2._doc_vectors, b1._doc_vectors)

    # Only new / edited documents are encoded; the cache grows.
    b3 = EmbeddingBackend("fake/model", cache_dir=tmp_path, encoder_factory=factory)
    b3.fit([*docs, "a brand new metric about refunds"])
    assert enc.calls[-1] == ["a brand new metric about refunds"]
    assert len(VectorCache(tmp_path, "fake/model").load()) == len(docs) + 1

    assert b3.score("refund")[2] > 0


def test_query_without_encoder_loads_lazily(tmp_path: Path) -> None:
    docs = [m.search_text() for m in METRICS]
    EmbeddingBackend("fake/model", cache_dir=tmp_path, encoder=FakeEncoder()).fit(docs)
    b = EmbeddingBackend("fake/model", cache_dir=tmp_path, encoder_factory=no_download)
    b.fit(docs)
    with pytest.raises(AssertionError, match="must not load a real model"):
        b.score("refund")


def test_cache_is_per_model_and_survives_corruption(tmp_path: Path) -> None:
    a = VectorCache(tmp_path, "org/model-a")
    b = VectorCache(tmp_path, "org/model-b")
    assert a.path != b.path and a.key("x") != b.key("x")
    a.path.write_bytes(b"not an npz")
    assert a.load() == {}
    backend = EmbeddingBackend("org/model-a", cache_dir=tmp_path, encoder=FakeEncoder())
    backend.fit(["churn"])  # rewrites the corrupt file
    assert len(a.load()) == 1
    assert not list(tmp_path.glob(".tmp-*"))


def test_hybrid_mixes_normalised_scores() -> None:
    backend, _ = dense()
    docs = [m.search_text() for m in METRICS]
    sparse = BM25Backend()
    sparse.fit(docs)
    backend.fit(docs)
    q = "refund rate"
    s, d = sparse.score(q), backend.score(q)
    h = HybridBackend(BM25Backend(), dense()[0], weight=0.25)
    h.fit(docs)
    expected = [0.75 * a / max(s) + 0.25 * b / max(d) for a, b in zip(s, d, strict=True)]
    assert h.score(q) == pytest.approx(expected)
    assert max(h.score(q)) == pytest.approx(1.0)


def test_hybrid_weight_extremes_and_validation() -> None:
    docs = [m.search_text() for m in METRICS]
    pure_bm25 = HybridBackend(BM25Backend(), dense()[0], weight=0.0)
    pure_bm25.fit(docs)
    enc = pure_bm25.dense._encoder  # type: ignore[attr-defined]
    n_calls = len(enc.calls)
    assert pure_bm25.score("people quitting") == [0.0] * len(METRICS)
    assert len(enc.calls) == n_calls  # weight 0: the dense model is not queried
    pure_dense = HybridBackend(BM25Backend(), dense()[0], weight=1.0)
    pure_dense.fit(docs)
    r = MetricRetriever(METRICS, backend=pure_dense)
    assert r.retrieve("people quitting", k=1)[0].metric.name == "attrition_rate"
    with pytest.raises(ValueError, match="weight"):
        HybridBackend(BM25Backend(), dense()[0], weight=1.5)


def test_make_backend_does_not_import_sentence_transformers(tmp_path: Path) -> None:
    sys.modules.pop("sentence_transformers", None)
    assert isinstance(make_backend("bm25"), BM25Backend)
    emb = make_backend("embedding", cache_dir=tmp_path)
    hyb = make_backend("hybrid", hybrid_weight=0.3)
    assert isinstance(emb, EmbeddingBackend) and not emb.encoder_loaded
    assert isinstance(hyb, HybridBackend) and hyb.weight == 0.3
    assert "sentence_transformers" not in sys.modules
    assert set(BACKENDS) == {"bm25", "embedding", "hybrid"}
    with pytest.raises(ValueError, match="unknown semantic backend"):
        make_backend("tfidf")
    fake = make_backend("embedding", encoder=FakeEncoder())
    assert isinstance(fake, EmbeddingBackend) and fake.encoder_loaded


def test_default_retriever_caches_per_backend_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "analyst_agent.semantic.embeddings.load_sentence_transformer", lambda name: FakeEncoder()
    )
    metrics_dir = Path(__file__).resolve().parents[2] / "benchmarks" / "metrics"
    a = default_retriever(metrics_dir)
    assert default_retriever(str(metrics_dir)) is a
    assert isinstance(a.backend, BM25Backend)
    h = default_retriever(metrics_dir, backend="hybrid", cache_dir=tmp_path, hybrid_weight=0.7)
    assert h is not a and isinstance(h.backend, HybridBackend) and h.backend.weight == 0.7
