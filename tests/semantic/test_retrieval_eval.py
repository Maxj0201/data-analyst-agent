"""benchmarks/metrics/retrieval_eval.yaml and ``python -m analyst_agent.semantic eval``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from analyst_agent.eval.schema import load_tasks
from analyst_agent.semantic import MetricRetriever, load_metrics
from analyst_agent.semantic.__main__ import app
from analyst_agent.semantic.evaluation import (
    DEFAULT_CASES,
    RetrievalCase,
    evaluate,
    format_reports,
    load_cases,
    metric_names,
)
from tests.semantic.test_embeddings import FakeEncoder

ROOT = Path(__file__).resolve().parents[2] / "benchmarks"
METRICS = load_metrics(ROOT / "metrics")


def test_metrics_dir_scan_skips_the_eval_file() -> None:
    assert DEFAULT_CASES.parent == ROOT / "metrics"
    assert len(METRICS) == 22  # the eval file is not parsed as metric definitions


def test_every_benchmark_task_is_mapped_to_known_metrics() -> None:
    cases = load_cases(DEFAULT_CASES, ROOT, metric_names(METRICS))
    task_ids = {t.id for t in load_tasks(ROOT)}
    bench = {c.id for c in cases if c.group == "benchmark"}
    assert bench == task_ids
    aov = next(c for c in cases if c.id == "l2_ecom_aov_by_segment")
    assert aov.expected == ["average_order_value"]
    assert set(aov.tables or []) == {"orders", "customers", "products"}
    assert any(c.group == "paraphrase" for c in cases)


def test_load_cases_validation(tmp_path: Path) -> None:
    def write(body: str) -> Path:
        p = tmp_path / "eval.yaml"
        p.write_text(body)
        return p

    with pytest.raises(ValueError, match="unknown metric"):
        load_cases(
            write(
                "kind: retrieval_eval\ncases:\n  - {id: a, question: q, tables: [t], "
                "expected: [nope]}\n"
            ),
            ROOT,
            metric_names(METRICS),
        )
    with pytest.raises(ValueError, match="unknown benchmark task"):
        load_cases(write("kind: retrieval_eval\ncases:\n  - {task: no_such_task}\n"), ROOT)
    with pytest.raises(ValueError, match="exactly one"):
        load_cases(write("kind: retrieval_eval\ncases:\n  - {expected: [x]}\n"), ROOT)
    with pytest.raises(ValueError, match="kind"):
        load_cases(write("kind: metrics\ncases:\n  - {task: x}\n"), ROOT)


def test_metrics_math() -> None:
    cases = [
        RetrievalCase("hit1", "average order value", ["orders"], ["average_order_value"], "p"),
        RetrievalCase("miss", "zzz", ["orders"], ["net_revenue"], "p"),
        RetrievalCase("none", "orders", ["orders"], [], "p"),
    ]
    rep = evaluate(MetricRetriever(METRICS), cases, "bm25")
    s = rep.summary()
    assert (s.n, s.unscored) == (2, 1)
    assert s.top1 == s.top3 == s.mrr == 0.5
    assert "MISS miss" in format_reports([rep], verbose=True)


def test_bm25_baseline_and_aov_fix() -> None:
    cases = load_cases(DEFAULT_CASES, ROOT, metric_names(METRICS))
    rep = evaluate(MetricRetriever(METRICS), cases, "bm25")
    aov = next(r for r in rep.results if r.case.id == "l2_ecom_aov_by_segment")
    assert aov.rank == 1
    raw = evaluate(MetricRetriever(METRICS, rerank_mentions=False), cases, "bm25")
    assert next(r for r in raw.results if r.case.id == "l2_ecom_aov_by_segment").rank != 1
    # Guard against retrieval regressions on the benchmark questions. Calibrated on the 80-task
    # benchmark (#15: 33 tasks with an intended metric; BM25 measures top-1 0.848, top-3 0.909,
    # MRR 0.896). The 30-task set measured top-1 >= 0.9; the new misses are "current employees"
    # questions ranking headcount 4th and retention questions ranking attrition 2nd-3rd.
    bench = rep.summary("benchmark")
    assert bench.top1 >= 0.84 and bench.top3 >= 0.9 and bench.mrr >= 0.88
    assert rep.summary().top1 >= raw.summary().top1


def test_cli_reports_every_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "analyst_agent.semantic.embeddings.load_sentence_transformer", lambda name: FakeEncoder()
    )
    res = CliRunner().invoke(app, ["eval", "--cache-dir", str(tmp_path), "--json"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert set(out) == {"bm25", "embedding", "hybrid"}
    for name in out:
        assert {"all", "benchmark", "paraphrase", "cases"} <= set(out[name])
        assert 0 <= out[name]["all"]["mrr"] <= 1
    res = CliRunner().invoke(app, ["eval", "-b", "bm25", "-v"])
    assert res.exit_code == 0 and "top-1" in res.output and "l2_ecom_aov_by_segment" in res.output


def test_cli_skips_backend_that_cannot_load(tmp_path: Path) -> None:
    # The autouse fixture makes loading a real model fail, as a missing extra would.
    res = CliRunner().invoke(
        app, ["eval", "-b", "bm25", "-b", "embedding", "--cache-dir", str(tmp_path)]
    )
    assert res.exit_code == 0, res.output
    assert "embedding  skipped" in res.output and "bm25" in res.output
    bad = CliRunner().invoke(app, ["eval", "-b", "tfidf"])
    assert bad.exit_code != 0
