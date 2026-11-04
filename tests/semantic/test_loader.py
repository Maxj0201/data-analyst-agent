from __future__ import annotations

from pathlib import Path

import pytest

from analyst_agent.semantic import Metric, MetricsLoadError, load_metrics, load_metrics_file

VALID = """\
version: 1
domain: shop
metrics:
  - name: net_revenue
    label: Net revenue
    definition: Sum of completed order value.
    sql_fragment: SELECT SUM(amount) AS net_revenue FROM orders WHERE status = 'completed';
    tables: [orders]
    synonyms: [revenue, sales]
    caveats: [Excludes refunds.]
  - name: order_count
    domain: ops
    definition: Number of orders.
    sql_fragment: |
      -- all statuses
      WITH o AS (SELECT * FROM orders) SELECT COUNT(*) FROM o
"""


def write(tmp_path: Path, text: str, name: str = "m.yaml") -> Path:
    p = tmp_path / name
    p.write_text(text)
    return p


def test_load_valid_file(tmp_path: Path) -> None:
    metrics = load_metrics_file(write(tmp_path, VALID))
    assert [m.name for m in metrics] == ["net_revenue", "order_count"]
    rev, cnt = metrics
    assert rev.domain == "shop"  # file-level default
    assert cnt.domain == "ops"  # per-metric override
    assert rev.sql_fragment.endswith("'completed'")  # trailing ';' stripped
    assert rev.synonyms == ["revenue", "sales"] and rev.caveats == ["Excludes refunds."]
    assert rev.display_name == "Net revenue" and cnt.display_name == "order count"
    assert cnt.tables == [] and cnt.caveats == []
    text = rev.search_text()
    assert "net revenue" in text and "sales" in text and "completed order" in text


def test_load_directory_and_mixed_paths(tmp_path: Path) -> None:
    write(tmp_path, VALID, "a.yaml")
    write(
        tmp_path,
        "domain: x\nmetrics:\n  - {name: other, definition: d, sql_fragment: SELECT 1}\n",
        "b.yml",
    )
    (tmp_path / "notes.txt").write_text("ignored")
    assert [m.name for m in load_metrics(tmp_path)] == ["net_revenue", "order_count", "other"]
    assert len(load_metrics([tmp_path / "b.yml"])) == 1


@pytest.mark.parametrize(
    ("metric_yaml", "message"),
    [
        ("{name: Bad Name, definition: d, sql_fragment: SELECT 1}", "snake_case"),
        ("{name: m, definition: d, sql_fragment: DELETE FROM t}", "read-only"),
        ("{name: m, definition: d, sql_fragment: 'SELECT 1; DROP TABLE t'}", "single statement"),
        ("{name: m, definition: '  ', sql_fragment: SELECT 1}", "blank"),
        ("{name: m, sql_fragment: SELECT 1}", "definition"),
        ("{name: m, definition: d, sql_fragment: SELECT 1, owner: me}", "Extra inputs"),
        ("{name: m, definition: d, sql_fragment: SELECT 1, synonyms: ['']}", "blank"),
    ],
)
def test_invalid_metric_rejected(tmp_path: Path, metric_yaml: str, message: str) -> None:
    p = write(tmp_path, f"domain: x\nmetrics:\n  - {metric_yaml}\n")
    with pytest.raises(MetricsLoadError, match=message):
        load_metrics_file(p)


def test_missing_domain_rejected(tmp_path: Path) -> None:
    p = write(tmp_path, "metrics:\n  - {name: m, definition: d, sql_fragment: SELECT 1}\n")
    with pytest.raises(MetricsLoadError, match="domain"):
        load_metrics_file(p)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("metrics: [", "invalid YAML"),
        ("- just a list", "mapping"),
        ("domain: x\nmetrics: []\n", "at least 1"),
        (
            "version: 2\ndomain: x\nmetrics:\n  - {name: m, definition: d, sql_fragment: SELECT 1}",
            "version",
        ),
        (
            "domain: x\nmetrics:\n"
            "  - {name: m, definition: d, sql_fragment: SELECT 1}\n"
            "  - {name: m, definition: e, sql_fragment: SELECT 2}\n",
            "duplicate",
        ),
    ],
)
def test_invalid_file_rejected(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(MetricsLoadError, match=message):
        load_metrics_file(write(tmp_path, text))


def test_duplicate_across_files_rejected(tmp_path: Path) -> None:
    write(tmp_path, VALID, "a.yaml")
    write(tmp_path, VALID, "b.yaml")
    with pytest.raises(MetricsLoadError, match="duplicate metric name 'net_revenue'"):
        load_metrics(tmp_path)


def test_missing_path_rejected(tmp_path: Path) -> None:
    with pytest.raises(MetricsLoadError, match="no such file"):
        load_metrics(tmp_path / "nope")


def test_metric_is_immutable() -> None:
    m = Metric(name="m", domain="d", definition="x", sql_fragment="SELECT 1")
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError on frozen model
        m.name = "other"  # type: ignore[misc]


def test_directory_scan_skips_other_kinds(tmp_path) -> None:
    from analyst_agent.semantic import MetricsLoadError, load_metrics, load_metrics_file

    (tmp_path / "m.yaml").write_text(
        "domain: d\nmetrics:\n  - {name: x, definition: d, sql_fragment: SELECT 1}\n"
    )
    other = tmp_path / "retrieval_eval.yaml"
    other.write_text("kind: retrieval_eval\ncases: []\n")
    assert [m.name for m in load_metrics(tmp_path)] == ["x"]
    import pytest

    with pytest.raises(MetricsLoadError):
        load_metrics([other])  # named explicitly: not silently skipped
    with pytest.raises(MetricsLoadError):
        load_metrics_file(other)
