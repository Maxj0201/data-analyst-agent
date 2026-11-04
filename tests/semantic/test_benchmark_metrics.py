"""The shipped benchmark metric definitions load, run against the CSVs and retrieve sensibly."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from analyst_agent import sandbox
from analyst_agent.semantic import MetricRetriever, load_metrics
from analyst_agent.tools import table_name_for

ROOT = Path(__file__).resolve().parents[2] / "benchmarks"
METRICS = load_metrics(ROOT / "metrics")
DATA = {table_name_for(p): p for p in (ROOT / "data").glob("*/*.csv")}


@pytest.fixture(scope="module")
def sb() -> Iterator[sandbox.Sandbox]:
    with sandbox.create_sandbox(DATA) as s:
        yield s


def test_coverage() -> None:
    assert len(METRICS) >= 15
    assert {m.domain for m in METRICS} == {"ecommerce", "saas", "hr"}
    names = {m.name for m in METRICS}
    for required in [
        "churn_rate",
        "arpu",
        "net_revenue_retention",
        "repeat_purchase_rate",
        "average_order_value",
        "return_rate",
        "attrition_rate",
        "pay_band_premium",
    ]:
        assert required in names
    for m in METRICS:
        assert m.tables, m.name
        assert set(m.tables) <= set(DATA), (m.name, m.tables)
        assert m.synonyms and m.caveats, m.name


@pytest.mark.parametrize("m", METRICS, ids=lambda m: m.name)
def test_sql_fragment_runs(sb: sandbox.Sandbox, m) -> None:
    res = sb.run_sql(m.sql_fragment)
    assert res.ok, res.error
    assert res.rows and all(v is not None for v in res.rows[0].values())
    for t in m.tables:  # declared tables are the ones actually read
        assert t in m.sql_fragment


def _value(sb: sandbox.Sandbox, name: str) -> float:
    m = next(m for m in METRICS if m.name == name)
    return float(next(iter(sb.run_sql(m.sql_fragment).rows[0].values())))


def test_fragments_match_benchmark_ground_truth(sb: sandbox.Sandbox) -> None:
    # Same definitions as tasks l2_ecom_net_revenue_completed, l2_saas_churn_rate_2025
    # and l3_hr_attrition.
    assert _value(sb, "net_revenue") == pytest.approx(854033.7075, rel=1e-6)
    assert _value(sb, "churn_rate") == pytest.approx(19.1159, rel=1e-4)
    assert _value(sb, "attrition_rate") == pytest.approx(100 * 60 / 376, rel=1e-6)
    assert _value(sb, "net_revenue_retention") + _value(sb, "revenue_churn_rate") == pytest.approx(
        100.0
    )


@pytest.mark.parametrize(
    ("question", "tables", "expected"),
    [
        ("What percentage of subscriptions churned during 2025?", ["subscriptions"], "churn_rate"),
        ("What is our ARPU?", ["subscriptions"], "arpu"),
        ("What is NRR for 2025?", ["subscriptions"], "net_revenue_retention"),
        ("How many customers are repeat buyers?", ["orders", "customers"], "repeat_purchase_rate"),
        ("What is the AOV?", ["orders", "customers", "products"], "average_order_value"),
        (  # l2_ecom_aov_by_segment: restates net revenue's formula, used to rank net_revenue 1st
            "What is the average order value (mean net revenue per completed order) for each "
            "customer segment? Net revenue = quantity * unit_price * (1 - discount_pct), "
            "counting only orders with status 'completed'.",
            ["orders", "customers", "products"],
            "average_order_value",
        ),
        ("Return rate by category", ["orders", "products"], "return_rate"),
        ("What was employee turnover last year?", ["employees"], "attrition_rate"),
        ("Which department has the biggest pay band premium?", ["employees"], "pay_band_premium"),
    ],
)
def test_retrieval_top1(question: str, tables: list[str], expected: str) -> None:
    hits = MetricRetriever(METRICS).retrieve(question, k=3, tables=tables)
    assert hits[0].metric.name == expected
    loaded = set(tables)
    assert all(set(h.metric.tables) <= loaded for h in hits)
