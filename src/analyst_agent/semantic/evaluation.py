"""Retrieval-quality eval for the semantic layer: top-1 / top-3 hit rate and MRR per backend.

Cases come from ``benchmarks/metrics/retrieval_eval.yaml`` (see that file for the format).
Run with ``python -m analyst_agent.semantic eval``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from analyst_agent.semantic.retriever import MetricRetriever
from analyst_agent.semantic.schema import Metric

BENCHMARKS = Path(__file__).resolve().parents[3] / "benchmarks"
DEFAULT_CASES = BENCHMARKS / "metrics" / "retrieval_eval.yaml"


class CaseSpec(BaseModel):
    """One YAML entry: either ``task:`` (a benchmark task id) or ``id:`` + ``question:``."""

    model_config = ConfigDict(extra="forbid")

    task: str | None = None
    id: str | None = None
    question: str | None = None
    tables: list[str] | None = None
    expected: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_source(self) -> CaseSpec:
        if (self.task is None) == (self.question is None):
            raise ValueError("a case needs exactly one of 'task' or 'question'")
        if self.question is not None and (self.id is None or self.tables is None):
            raise ValueError("a 'question' case needs 'id' and 'tables'")
        return self


class EvalFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    version: int = 1
    cases: list[CaseSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> EvalFile:
        if self.kind != "retrieval_eval":
            raise ValueError(f"expected kind: retrieval_eval, got {self.kind!r}")
        return self


@dataclass(frozen=True)
class RetrievalCase:
    id: str
    question: str
    tables: list[str] | None
    expected: list[str]
    group: str  # "benchmark" (from a task) or "paraphrase"


def load_cases(
    path: Path | str = DEFAULT_CASES,
    benchmarks: Path | str = BENCHMARKS,
    metric_names: set[str] | None = None,
) -> list[RetrievalCase]:
    """Resolve ``task:`` entries against the benchmark tasks and validate metric names."""
    from analyst_agent.eval.schema import load_tasks
    from analyst_agent.tools import table_name_for

    spec = EvalFile.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
    needed = {c.task for c in spec.cases if c.task}
    tasks = {t.id: t for t in load_tasks(Path(benchmarks), ids=needed)} if needed else {}
    cases: list[RetrievalCase] = []
    seen: set[str] = set()
    for c in spec.cases:
        if c.task is not None:
            if c.task not in tasks:
                raise ValueError(f"{path}: unknown benchmark task {c.task!r}")
            t = tasks[c.task]
            tables = c.tables or [table_name_for(p) for p in t.dataset_paths()]
            case = RetrievalCase(c.id or t.id, t.question, tables, c.expected, "benchmark")
        else:
            assert c.id is not None and c.question is not None
            case = RetrievalCase(c.id, c.question, c.tables, c.expected, "paraphrase")
        if case.id in seen:
            raise ValueError(f"{path}: duplicate case id {case.id!r}")
        seen.add(case.id)
        if metric_names is not None and (bad := set(case.expected) - metric_names):
            raise ValueError(f"{path}: case {case.id!r} expects unknown metric(s) {sorted(bad)}")
        cases.append(case)
    return cases


@dataclass
class CaseResult:
    case: RetrievalCase
    ranked: list[str]  # retrieved metric names, best first
    rank: int | None  # 1-based rank of the first expected metric, None if not retrieved

    @property
    def scored(self) -> bool:
        return bool(self.case.expected)


@dataclass
class Summary:
    n: int  # scored cases (non-empty `expected`)
    top1: float
    top3: float
    mrr: float
    unscored: int = 0


@dataclass
class BackendReport:
    backend: str
    results: list[CaseResult] = field(default_factory=list)
    error: str | None = None  # set when the backend could not run (e.g. extra not installed)

    def summary(self, group: str | None = None) -> Summary:
        rs = [r for r in self.results if group is None or r.case.group == group]
        scored = [r for r in rs if r.scored]
        n = len(scored)
        if not n:
            return Summary(0, 0.0, 0.0, 0.0, len(rs))
        return Summary(
            n=n,
            top1=sum(r.rank == 1 for r in scored) / n,
            top3=sum(r.rank is not None and r.rank <= 3 for r in scored) / n,
            mrr=sum(1 / r.rank for r in scored if r.rank) / n,
            unscored=len(rs) - n,
        )


def evaluate(
    retriever: MetricRetriever, cases: Sequence[RetrievalCase], backend: str = ""
) -> BackendReport:
    """Rank all eligible metrics for each case (no top-k cut, so MRR sees every rank)."""
    report = BackendReport(backend)
    k = max(1, len(retriever.metrics))
    for c in cases:
        ranked = [h.metric.name for h in retriever.retrieve(c.question, k=k, tables=c.tables)]
        rank = next((i + 1 for i, name in enumerate(ranked) if name in c.expected), None)
        report.results.append(CaseResult(c, ranked, rank))
    return report


def format_reports(reports: Sequence[BackendReport], verbose: bool = False) -> str:
    groups = sorted({r.case.group for rep in reports for r in rep.results})
    lines = [f"{'backend':<10} {'group':<11} {'cases':>5} {'top-1':>6} {'top-3':>6} {'MRR':>6}"]
    lines.append("-" * len(lines[0]))
    for rep in reports:
        if rep.error:
            lines.append(f"{rep.backend:<10} skipped: {rep.error}")
            continue
        for g in [None, *groups] if len(groups) > 1 else [None]:
            s = rep.summary(g)
            lines.append(
                f"{rep.backend:<10} {g or 'all':<11} {s.n:>5} "
                f"{s.top1:>6.3f} {s.top3:>6.3f} {s.mrr:>6.3f}"
            )
    unscored = next((rep.summary().unscored for rep in reports if not rep.error), 0)
    if unscored:
        lines.append(f"({unscored} cases with no intended metric are not scored)")
    if verbose:
        for rep in reports:
            if rep.error:
                continue
            lines += ["", f"[{rep.backend}] scored cases:"]
            for r in rep.results:
                if not r.scored:
                    continue
                mark = "ok  " if r.rank == 1 else ("top3" if r.rank and r.rank <= 3 else "MISS")
                lines.append(
                    f"  {mark} {r.case.id:<34} rank={r.rank or '-':<3} "
                    f"expected={','.join(r.case.expected)}  got={','.join(r.ranked[:3]) or '-'}"
                )
    return "\n".join(lines)


def report_dict(reports: Sequence[BackendReport]) -> dict[str, object]:
    out: dict[str, object] = {}
    for rep in reports:
        if rep.error:
            out[rep.backend] = {"error": rep.error}
            continue
        groups = sorted({r.case.group for r in rep.results})
        out[rep.backend] = {g or "all": vars(rep.summary(g)) for g in [None, *groups]} | {
            "cases": [
                {"id": r.case.id, "expected": r.case.expected, "rank": r.rank, "top3": r.ranked[:3]}
                for r in rep.results
                if r.scored
            ]
        }
    return out


def metric_names(metrics: Sequence[Metric]) -> set[str]:
    return {m.name for m in metrics}
