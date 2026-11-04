from __future__ import annotations

from collections.abc import Sequence

from analyst_agent.semantic import BM25Backend, Metric, MetricRetriever, ScoringBackend, tokenize


def metric(name: str, definition: str, synonyms: list[str], tables: list[str]) -> Metric:
    return Metric(
        name=name,
        domain="test",
        definition=definition,
        sql_fragment="SELECT 1",
        synonyms=synonyms,
        tables=tables,
    )


METRICS = [
    metric("churn_rate", "Share of subscriptions that cancel.", ["logo churn"], ["subscriptions"]),
    metric("active_mrr", "Monthly recurring revenue of active subs.", ["mrr"], ["subscriptions"]),
    metric("return_rate", "Share of orders returned.", ["refund rate"], ["orders"]),
    metric("attrition_rate", "Employee terminations over headcount.", ["turnover"], ["employees"]),
    metric("global_note", "A metric with no table dependency about churn.", [], []),
]


def test_tokenize() -> None:
    assert tokenize("What is the Churn-Rate for subscriptions in 2025?") == [
        "churn",
        "rate",
        "subscription",
        "2025",
    ]
    assert tokenize("the of and") == []
    assert tokenize("class business") == ["class", "business"]  # no 'ss' stripping


def test_bm25_ranks_by_relevance() -> None:
    r = MetricRetriever(METRICS)
    hits = r.retrieve("What was our logo churn last year?", k=2)
    assert [h.metric.name for h in hits][0] == "churn_rate"
    assert hits[0].score > hits[1].score > 0
    assert r.retrieve("employee turnover", k=1)[0].metric.name == "attrition_rate"
    assert r.retrieve("refund rate by category", k=1)[0].metric.name == "return_rate"


def test_k_and_no_match() -> None:
    r = MetricRetriever(METRICS)
    assert r.retrieve("churn", k=0) == []
    assert r.retrieve("zzz qqq", k=5) == []  # zero score never returned
    assert r.retrieve("", k=5) == []
    assert len(r.retrieve("rate", k=2)) == 2


def test_table_filter() -> None:
    r = MetricRetriever(METRICS)
    names = [h.metric.name for h in r.retrieve("churn", k=5, tables=["orders"])]
    # churn_rate needs subscriptions -> filtered; table-less metrics are always eligible.
    assert names == ["global_note"]
    names = [h.metric.name for h in r.retrieve("churn", k=5, tables=["SUBSCRIPTIONS"])]
    assert names[0] == "churn_rate" and "global_note" in names


def test_empty_corpus() -> None:
    assert MetricRetriever([]).retrieve("churn") == []
    b = BM25Backend()
    b.fit([])
    assert b.score("churn") == []


class KeywordEmbeddingBackend:
    """Stand-in for an embedding backend: cosine-ish overlap on a fixed vocabulary."""

    def __init__(self) -> None:
        self.docs: list[set[str]] = []
        self.fitted_with: list[str] = []

    def fit(self, documents: Sequence[str]) -> None:
        self.fitted_with = list(documents)
        self.docs = [set(tokenize(d)) for d in documents]

    def score(self, query: str) -> list[float]:
        q = set(tokenize(query))
        return [len(q & d) / (len(d) or 1) for d in self.docs]


def test_backend_is_swappable() -> None:
    backend = KeywordEmbeddingBackend()
    assert isinstance(backend, ScoringBackend)
    assert isinstance(BM25Backend(), ScoringBackend)
    r = MetricRetriever(METRICS, backend=backend)
    assert len(backend.fitted_with) == len(METRICS)
    assert "logo churn" in backend.fitted_with[0]
    assert r.retrieve("employee turnover headcount", k=1)[0].metric.name == "attrition_rate"


def test_min_score() -> None:
    r = MetricRetriever(METRICS, min_score=1e9)
    assert r.retrieve("churn") == []


COMPOSITE = [
    metric(
        "net_revenue",
        "Sum of quantity * unit_price * (1 - discount_pct) for completed orders.",
        ["revenue", "sales"],
        ["orders"],
    ),
    metric(
        "average_order_value",
        "Mean net revenue per completed order.",
        ["aov", "basket size"],
        ["orders"],
    ),
    metric(
        "net_revenue_retention",
        "MRR retained from existing accounts.",
        ["nrr"],
        ["subscriptions"],
    ),
    metric("active_mrr", "MRR of active subs.", ["run rate", "recurring revenue"], ["subs"]),
    metric("arr", "Annual MRR.", ["annual run rate", "annual recurring revenue"], ["subs"]),
]

# The l2_ecom_aov_by_segment question: names AOV but restates net revenue's formula.
AOV_Q = (
    "What is the average order value (mean net revenue per completed order) for each customer "
    "segment? Net revenue = quantity * unit_price * (1 - discount_pct), counting only orders "
    "with status 'completed'."
)


def test_mention_rerank_ranks_composite_above_component() -> None:
    raw = MetricRetriever(COMPOSITE, rerank_mentions=False).retrieve(AOV_Q, k=2)
    assert raw[0].metric.name == "net_revenue"  # the bug being fixed
    hits = MetricRetriever(COMPOSITE).retrieve(AOV_Q, k=2)
    assert [h.metric.name for h in hits] == ["average_order_value", "net_revenue"]
    assert all(h.mentioned for h in hits)
    assert hits[0].score < hits[1].score  # backend scores are reported unchanged


def test_mention_rerank_leaves_plain_component_questions_alone() -> None:
    q = "What is the total net revenue? Net revenue = quantity * unit_price, completed orders."
    assert MetricRetriever(COMPOSITE).retrieve(q, k=1)[0].metric.name == "net_revenue"


def test_mention_longest_match_and_distinctive_phrases() -> None:
    r = MetricRetriever(COMPOSITE)
    nrr = r.retrieve("net revenue retention in 2025", k=3)
    assert nrr[0].metric.name == "net_revenue_retention" and nrr[0].mentioned
    # "net revenue" is inside "net revenue retention": not a mention of net_revenue here.
    assert not any(h.mentioned for h in nrr if h.metric.name == "net_revenue")
    # "run rate" is also part of ARR's "annual run rate", so it does not single out active_mrr.
    hits = r.retrieve("what is our run rate", k=2)
    assert not any(h.mentioned for h in hits)


def test_mention_rerank_respects_table_filter() -> None:
    hits = MetricRetriever(COMPOSITE).retrieve(AOV_Q, k=5, tables=["subscriptions"])
    assert [h.metric.name for h in hits] == ["net_revenue_retention"]
