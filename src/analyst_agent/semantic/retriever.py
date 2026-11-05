"""Retrieve the metric definitions most relevant to a question.

The retriever is backend-agnostic: anything implementing :class:`ScoringBackend` can rank
documents. :class:`BM25Backend` (``rank_bm25``) is the default; ``embeddings.EmbeddingBackend``
(sentence-transformers) and ``embeddings.HybridBackend`` (weighted BM25 + embedding) implement
the same ``fit`` / ``score`` protocol. Pick one by name with
:func:`analyst_agent.semantic.make_backend`.

On top of the backend score, :class:`MetricRetriever` applies a backend-independent *mention
rerank*: a metric whose name, label or a multi-word synonym appears verbatim in the question gets
a score boost over metrics that merely share vocabulary with it, and when two mentioned metrics are
related (one's definition names the other, e.g. AOV is "net revenue per completed order") the
composite metric outranks its component. Without it, a question that spells out a component's
formula ("average order value ... Net revenue = quantity * unit_price ...") ranks the component
first.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from rank_bm25 import BM25Okapi

from analyst_agent.semantic.schema import Metric

if TYPE_CHECKING:
    from analyst_agent.semantic.vocabulary import Vocabulary

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
    """Okapi BM25 (``rank_bm25``) over :func:`tokenize`-d documents, with a non-negative IDF.

    With a ``vocabulary`` (see :mod:`analyst_agent.semantic.vocabulary`), the tokens it adds for
    a question are scored as extra query terms at ``expansion_weight`` (BM25 is a sum over query
    terms, so this is ``score(question) + expansion_weight * score(expansion)``).
    """

    def __init__(
        self,
        k1: float = 1.5,
        b: float = 0.75,
        *,
        vocabulary: Vocabulary | None = None,
        expansion_weight: float = 0.5,
    ) -> None:
        if expansion_weight < 0:
            raise ValueError(f"expansion_weight must be >= 0, got {expansion_weight}")
        self.k1 = k1
        self.b = b
        self.vocabulary = vocabulary
        self.expansion_weight = expansion_weight
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
        scores = self._bm25.get_scores(q)
        extra = self.vocabulary.expand(q) if self.vocabulary else []
        if extra and self.expansion_weight > 0:
            scores = scores + self.expansion_weight * self._bm25.get_scores(extra)
        return [float(s) for s in scores]


@dataclass(frozen=True)
class RetrievedMetric:
    metric: Metric
    score: float  # backend score; with the mention rerank, order is not always by score
    mentioned: bool = False  # the question names this metric verbatim (see module docstring)


_PAREN_RE = re.compile(r"\(([^)]*)\)")


def _contains(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    n = len(needle)
    return n > 0 and any(
        tuple(haystack[i : i + n]) == tuple(needle) for i in range(len(haystack) - n + 1)
    )


def mention_phrases(m: Metric) -> list[tuple[str, ...]]:
    """Token phrases that count as naming ``m`` explicitly in a question.

    The metric name (any length), plus label parts and synonyms of two or more tokens. One-word
    synonyms ("revenue", "tenure", "employees") are too generic to count as a mention; BM25 and
    embeddings still match on them. :class:`_MentionIndex` further drops phrases shared with
    another metric.
    """
    label = m.label or ""
    label_parts = [*_PAREN_RE.findall(label), *_PAREN_RE.sub(" ", label).split("/")]
    out: list[tuple[str, ...]] = []
    name = tuple(tokenize(m.name.replace("_", " ")))
    if name:
        out.append(name)
    for text in [*label_parts, *m.synonyms]:
        toks = tuple(tokenize(text))
        if len(toks) >= 2 and toks not in out:
            out.append(toks)
    return out


class _MentionIndex:
    """Which metrics a question names, and which named metrics are built on which."""

    def __init__(self, metrics: Sequence[Metric]) -> None:
        candidates = [mention_phrases(m) for m in metrics]
        names = [tokenize(m.name.replace("_", " ")) for m in metrics]
        # Synonym / label phrases must be distinctive: one that also occurs inside another
        # metric's phrase ("run rate" vs ARR's "annual run rate", "cancellation rate" as a churn
        # synonym) does not identify this metric. Metric names always count.
        self.phrases = [
            [
                p
                for p in phrases
                if list(p) == names[i]
                or not any(_contains(q, p) for j, qs in enumerate(candidates) if j != i for q in qs)
            ]
            for i, phrases in enumerate(candidates)
        ]
        definitions = [tokenize(m.definition) for m in metrics]
        # composite_of[i] = indices j whose name appears in metric i's definition (i uses j).
        self.composite_of: list[set[int]] = [
            {
                j
                for j, other in enumerate(metrics)
                if j != i and _contains(definitions[i], tokenize(other.name.replace("_", " ")))
            }
            for i in range(len(metrics))
        ]

    def mentioned(self, question: str) -> set[int]:
        q = tokenize(question)
        spans: dict[int, list[tuple[int, int]]] = {}
        for i, phrases in enumerate(self.phrases):
            for p in phrases:
                n = len(p)
                for start in range(len(q) - n + 1):
                    if tuple(q[start : start + n]) == p:
                        spans.setdefault(i, []).append((start, start + n))
        # Longest match wins: "net revenue retention" is a mention of NRR, not of net revenue.
        # A metric stays mentioned if any of its spans is not strictly inside another's.
        all_spans = [(s, e, i) for i, ss in spans.items() for s, e in ss]

        def covered(s: int, e: int, i: int) -> bool:
            return any(
                j != i and s2 <= s and e <= e2 and (e2 - s2) > (e - s) for s2, e2, j in all_spans
            )

        return {i for i, ss in spans.items() if any(not covered(s, e, i) for s, e in ss)}


class MetricRetriever:
    """Rank metric definitions against a question.

    ``tables`` (optional, per call) restricts results to metrics whose ``tables`` are all
    loaded, so a SaaS question never gets HR definitions that reference missing tables.
    Metrics that declare no tables are always eligible.

    ``rerank_mentions`` (default on) applies the mention rerank described in the module
    docstring: a mentioned metric's score is multiplied by ``mention_boost`` (scale-free, so it
    works the same for BM25, cosine and hybrid scores), then no mentioned metric may outrank a
    mentioned composite whose definition names it. Turn it off to see the raw backend ranking.
    """

    def __init__(
        self,
        metrics: Iterable[Metric],
        backend: ScoringBackend | None = None,
        *,
        min_score: float = 0.0,
        rerank_mentions: bool = True,
        mention_boost: float = 1.5,
    ) -> None:
        self.metrics: list[Metric] = list(metrics)
        self.backend: ScoringBackend = backend or BM25Backend()
        self.min_score = min_score
        self.rerank_mentions = rerank_mentions
        self.mention_boost = mention_boost
        self._mentions = _MentionIndex(self.metrics)
        self.backend.fit([m.search_text() for m in self.metrics])

    def retrieve(
        self, question: str, k: int = 5, tables: Iterable[str] | None = None
    ) -> list[RetrievedMetric]:
        if k <= 0 or not self.metrics:
            return []
        scores = self.backend.score(question)
        loaded = {t.lower() for t in tables} if tables is not None else None
        eligible = [
            i
            for i, (m, s) in enumerate(zip(self.metrics, scores, strict=True))
            if s > self.min_score and (loaded is None or all(t.lower() in loaded for t in m.tables))
        ]
        mentioned = self._mentions.mentioned(question) if self.rerank_mentions else set()
        # Only metrics that survive the table filter take part in the rerank.
        mentioned &= set(eligible)
        eff = {i: scores[i] * (self.mention_boost if i in mentioned else 1.0) for i in eligible}
        # A mentioned component never outranks a mentioned composite built on it. Iterate to a
        # fixed point so chains (A uses B uses C) settle; bounded by the number of metrics.
        for _ in range(len(mentioned)):
            changed = False
            for i in mentioned:
                for j in self._mentions.composite_of[i] & mentioned:
                    if eff[j] >= eff[i]:
                        eff[j] = math.nextafter(eff[i], -math.inf)
                        changed = True
            if not changed:
                break

        # Stable sort: ties keep file order.
        eligible.sort(key=lambda i: eff[i], reverse=True)
        return [
            RetrievedMetric(self.metrics[i], float(scores[i]), i in mentioned) for i in eligible[:k]
        ]
