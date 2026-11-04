"""Retrieve the metric definitions most relevant to a question.

The retriever is backend-agnostic: anything implementing :class:`ScoringBackend` can rank
documents. :class:`BM25Backend` (``rank_bm25``) is the default; an embedding backend only needs
``fit`` and ``score`` with the same signatures.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from rank_bm25 import BM25Okapi

from analyst_agent.semantic.schema import Metric

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Small stop-word list: question words that would otherwise dominate BM25 on short queries.
STOPWORDS = frozenset(
    "a an and are as at be by did do does for from how in is it of on or per the to was "
    "were what which who why with each our we you me my this that these those than across "
    "all any".split()
)


def tokenize(text: str) -> list[str]:
    """Lower-case alphanumeric tokens with stop words removed and a light plural strip."""
    out = []
    for t in _TOKEN_RE.findall(text.lower()):
        if t in STOPWORDS:
            continue
        if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.append(t)
    return out


@runtime_checkable
class ScoringBackend(Protocol):
    """Scores a query against a fixed corpus. Higher is more relevant; 0 means no match."""

    def fit(self, documents: Sequence[str]) -> None: ...

    def score(self, query: str) -> list[float]: ...


class _NonNegativeBM25(BM25Okapi):
    """BM25Okapi with the Lucene IDF ``log(1 + (N - n + 0.5) / (n + 0.5))``.

    The classic Okapi IDF is zero or negative for terms in half or more of the documents,
    which on a small metrics catalogue makes an exact name match score 0. This IDF is always
    positive, so a document scores > 0 iff it shares a term with the query.
    """

    def _calc_idf(self, nd: dict[str, int]) -> None:
        for word, freq in nd.items():
            self.idf[word] = math.log(1 + (self.corpus_size - freq + 0.5) / (freq + 0.5))


class BM25Backend:
    """Okapi BM25 (``rank_bm25``) over :func:`tokenize`-d documents, with a non-negative IDF."""

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self._bm25: _NonNegativeBM25 | None = None
        self._n = 0

    def fit(self, documents: Sequence[str]) -> None:
        self._n = len(documents)
        corpus = [tokenize(d) or ["_empty_"] for d in documents]
        self._bm25 = _NonNegativeBM25(corpus, k1=self.k1, b=self.b) if corpus else None

    def score(self, query: str) -> list[float]:
        if self._bm25 is None:
            return [0.0] * self._n
        q = tokenize(query)
        if not q:
            return [0.0] * self._n
        return [float(s) for s in self._bm25.get_scores(q)]


@dataclass(frozen=True)
class RetrievedMetric:
    metric: Metric
    score: float


class MetricRetriever:
    """Rank metric definitions against a question.

    ``tables`` (optional, per call) restricts results to metrics whose ``tables`` are all
    loaded, so a SaaS question never gets HR definitions that reference missing tables.
    Metrics that declare no tables are always eligible.
    """

    def __init__(
        self,
        metrics: Iterable[Metric],
        backend: ScoringBackend | None = None,
        *,
        min_score: float = 0.0,
    ) -> None:
        self.metrics: list[Metric] = list(metrics)
        self.backend: ScoringBackend = backend or BM25Backend()
        self.min_score = min_score
        self.backend.fit([m.search_text() for m in self.metrics])

    def retrieve(
        self, question: str, k: int = 5, tables: Iterable[str] | None = None
    ) -> list[RetrievedMetric]:
        if k <= 0 or not self.metrics:
            return []
        scores = self.backend.score(question)
        loaded = {t.lower() for t in tables} if tables is not None else None
        hits = [
            RetrievedMetric(m, s)
            for m, s in zip(self.metrics, scores, strict=True)
            if s > self.min_score and (loaded is None or all(t.lower() in loaded for t in m.tables))
        ]
        # Stable sort: ties keep file order.
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]
