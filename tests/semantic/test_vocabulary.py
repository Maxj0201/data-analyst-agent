"""Analyst vocabulary (BM25 query expansion): file format, matching, backend plumbing."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from analyst_agent.eval.schema import load_tasks
from analyst_agent.semantic import (
    BM25Backend,
    HybridBackend,
    MetricRetriever,
    MetricsLoadError,
    Vocabulary,
    default_retriever,
    load_metrics,
    load_vocabulary,
    make_backend,
    tokenize,
)
from analyst_agent.semantic.__main__ import app
from analyst_agent.semantic.vocabulary import ConceptSpec

ROOT = Path(__file__).resolve().parents[2] / "benchmarks"
METRICS_DIR = ROOT / "metrics"
VOCAB = load_vocabulary(METRICS_DIR)


def vocab(*specs: dict[str, object]) -> Vocabulary:
    return Vocabulary.from_specs(ConceptSpec.model_validate(s) for s in specs)


def test_shipped_vocabulary_loads_and_metrics_loader_skips_it() -> None:
    assert {c.name for c in VOCAB.concepts} >= {"headcount", "churn_attrition", "revenue"}
    assert len(load_metrics(METRICS_DIR)) == 22


def test_shipped_vocabulary_is_short_generic_phrases() -> None:
    """A phrase table, not memorised questions: short phrases, none a whole benchmark question."""
    questions = {tuple(tokenize(t.question)) for t in load_tasks(ROOT)}
    for c in VOCAB.concepts:
        for phrase in c.triggers:
            assert 1 <= len(phrase) <= 3, (c.name, phrase)
            assert phrase not in questions


def test_expand_matches_tokens_phrases_and_skips_present_terms() -> None:
    v = vocab(
        {"name": "hc", "expands_to": ["headcount", "workforce"], "phrases": ["current employees"]},
        {"name": "rev", "expands_to": ["revenue", "sales"], "phrases": ["gmv"]},
    )
    # Plural strip and stop words: "employees" matches "employee"; "the" is ignored.
    assert v.expand(tokenize("How many of the current employees are remote?")) == [
        "headcount",
        "workforce",
    ]
    assert v.matched(tokenize("current employees")) == ["hc"]
    # Phrases match only as contiguous token runs.
    assert v.expand(tokenize("employees who are current")) == []
    # An expands_to term also triggers its concept; terms already asked for are not re-added.
    assert v.expand(tokenize("revenue by month")) == ["sale"]
    assert v.expand(tokenize("GMV and headcount")) == ["workforce", "revenue", "sale"]
    assert v.expand([]) == [] and not vocab() and len(v) == 2


def test_bm25_expansion_is_weighted_and_optional() -> None:
    docs = ["headcount number of active employees", "average tenure of current employees"]
    v = vocab({"name": "hc", "expands_to": ["headcount"], "phrases": ["current employees"]})
    plain, expanded, off = (
        BM25Backend(),
        BM25Backend(vocabulary=v),
        BM25Backend(vocabulary=v, expansion_weight=0),
    )
    for b in (plain, expanded, off):
        b.fit(docs)
    q = "current employees"
    assert plain.score(q)[1] > plain.score(q)[0]  # tenure wins on raw overlap
    # The phrase table adds half-weight "headcount" for this phrasing: only doc 0 gains.
    assert expanded.score(q)[0] > plain.score(q)[0] and expanded.score(q)[1] == plain.score(q)[1]
    strong = BM25Backend(vocabulary=v, expansion_weight=3.0)
    strong.fit(docs)
    assert strong.score(q)[0] > strong.score(q)[1]
    assert off.score(q) == plain.score(q)
    assert expanded.score("unrelated words") == plain.score("unrelated words")
    with pytest.raises(ValueError, match="expansion_weight"):
        BM25Backend(expansion_weight=-0.1)


def test_make_backend_and_default_retriever_carry_the_vocabulary() -> None:
    bm25 = make_backend("bm25", vocabulary=VOCAB)
    assert isinstance(bm25, BM25Backend) and bm25.vocabulary is VOCAB
    hybrid = make_backend("hybrid", vocabulary=VOCAB)
    assert isinstance(hybrid, HybridBackend) and hybrid.sparse.vocabulary is VOCAB  # type: ignore[attr-defined]
    r = default_retriever(METRICS_DIR)
    assert isinstance(r.backend, BM25Backend) and r.backend.vocabulary
    hit = r.retrieve("How many current employees work in Sales?", k=1, tables=["employees"])
    assert hit[0].metric.name == "headcount"


def test_retriever_with_vocabulary_ranks_analyst_phrasings() -> None:
    r = MetricRetriever(load_metrics(METRICS_DIR), BM25Backend(vocabulary=VOCAB))
    top = r.retrieve("What was first-year retention of 2023 hires?", k=1, tables=["employees"])
    assert top[0].metric.name == "attrition_rate"
    top = r.retrieve("What share of subscriptions were retained?", k=1, tables=["subscriptions"])
    assert top[0].metric.name == "churn_rate"


def test_load_vocabulary_validation(tmp_path: Path) -> None:
    def write(name: str, body: str) -> Path:
        p = tmp_path / name
        p.write_text(body)
        return p

    ok = "kind: vocabulary\nconcepts:\n  - {name: a, expands_to: [x], phrases: [y]}\n"
    write("a.yaml", ok)
    write("metrics.yaml", "version: 1\nmetrics: []\n")  # another kind: ignored
    assert [c.name for c in load_vocabulary(tmp_path).concepts] == ["a"]
    # An explicitly passed non-vocabulary file contributes nothing.
    assert not load_vocabulary(tmp_path / "metrics.yaml")
    write("b.yml", ok)
    with pytest.raises(MetricsLoadError, match="duplicate vocabulary concept"):
        load_vocabulary(tmp_path)
    with pytest.raises(MetricsLoadError, match="no such file"):
        load_vocabulary(tmp_path / "missing")
    bad = tmp_path / "bad"
    bad.mkdir()
    for body, match in [
        ("kind: vocabulary\nconcepts:\n  - {name: a, expands_to: [the]}\n", "stop words"),
        ("kind: vocabulary\nconcepts:\n  - {name: a, expands_to: []}\n", "expands_to"),
        ("kind: vocabulary\nversion: 2\nconcepts:\n  - {name: a, expands_to: [x]}\n", "version"),
        ("kind: vocabulary\nconcepts:\n  - {name: a, expands_to: [x], bogus: 1}\n", "bogus"),
        ("kind: vocabulary\nconcepts: [\n", "invalid YAML"),
    ]:
        f = write("bad/v.yaml", body)
        with pytest.raises(MetricsLoadError, match=match):
            load_vocabulary(f)


def test_vocabulary_file_kind_is_checked() -> None:
    from analyst_agent.semantic.vocabulary import VocabularyFile

    with pytest.raises(ValueError, match="expected kind"):
        VocabularyFile.model_validate({"kind": "metrics", "concepts": [{"name": "a"}]})


def test_cli_vocabulary_switch() -> None:
    on = CliRunner().invoke(app, ["eval", "-b", "bm25", "--json"])
    off = CliRunner().invoke(app, ["eval", "-b", "bm25", "--json", "--no-vocabulary"])
    assert on.exit_code == 0 and off.exit_code == 0, (on.output, off.output)
    assert on.output != off.output
