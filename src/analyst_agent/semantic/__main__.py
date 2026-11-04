"""``python -m analyst_agent.semantic eval``: retrieval quality per scoring backend."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from analyst_agent.core.config import Settings
from analyst_agent.semantic.backends import BACKENDS, make_backend
from analyst_agent.semantic.evaluation import (
    BENCHMARKS,
    DEFAULT_CASES,
    BackendReport,
    evaluate,
    format_reports,
    load_cases,
    metric_names,
    report_dict,
)
from analyst_agent.semantic.loader import load_metrics
from analyst_agent.semantic.retriever import MetricRetriever

app = typer.Typer(help="Semantic layer tools.", no_args_is_help=True)


@app.callback()
def _main() -> None:
    """Semantic layer tools."""


@app.command("eval")
def eval_cmd(
    backend: Annotated[
        list[str] | None,
        typer.Option("--backend", "-b", help=f"one of {', '.join(BACKENDS)}; repeatable"),
    ] = None,
    cases: Annotated[Path, typer.Option(help="retrieval eval YAML")] = DEFAULT_CASES,
    metrics_dir: Annotated[
        Path | None, typer.Option(help="default AGENT_SEMANTIC_METRICS_DIR")
    ] = None,
    benchmarks: Annotated[Path, typer.Option(help="benchmark root (tasks/)")] = BENCHMARKS,
    hybrid_weight: Annotated[
        float | None, typer.Option(help="embedding share; default AGENT_SEMANTIC_HYBRID_WEIGHT")
    ] = None,
    embedding_model: Annotated[str | None, typer.Option()] = None,
    cache_dir: Annotated[Path | None, typer.Option(help="embedding vector cache")] = None,
    rerank: Annotated[
        bool, typer.Option("--rerank/--no-rerank", help="mention rerank (see retriever.py)")
    ] = True,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="per-case ranks")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="machine-readable output")] = False,
) -> None:
    """Report top-1 / top-3 hit rate and MRR of metric retrieval for each backend.

    Embedding backends that cannot load (extra not installed, model not downloadable) are
    reported as skipped rather than failing the whole run.
    """
    s = Settings()
    names = backend or list(BACKENDS)
    for n in names:
        if n not in BACKENDS:
            raise typer.BadParameter(f"unknown backend {n!r}; expected one of {BACKENDS}")
    metrics = load_metrics(metrics_dir or s.semantic_metrics_dir)
    case_list = load_cases(cases, benchmarks, metric_names(metrics))
    reports: list[BackendReport] = []
    for n in names:
        try:
            retriever = MetricRetriever(
                metrics,
                make_backend(
                    n,
                    embedding_model=embedding_model or s.semantic_embedding_model,
                    cache_dir=cache_dir or s.semantic_embedding_cache_dir,
                    hybrid_weight=s.semantic_hybrid_weight
                    if hybrid_weight is None
                    else hybrid_weight,
                ),
                rerank_mentions=rerank,
            )
            reports.append(evaluate(retriever, case_list, n))
        except Exception as e:  # noqa: BLE001 - report and continue with other backends
            reports.append(BackendReport(n, error=f"{type(e).__name__}: {e}"))
    if as_json:
        typer.echo(json.dumps(report_dict(reports), indent=2))
    else:
        typer.echo(format_reports(reports, verbose=verbose))


if __name__ == "__main__":
    app()
