"""Eval CLI: ``python -m analyst_agent.eval {run,report,compare,list}``."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from analyst_agent.eval.agent import load_agent
from analyst_agent.eval.config import EvalSettings
from analyst_agent.eval.judge import LLMJudge
from analyst_agent.eval.report import compare_runs, report_run
from analyst_agent.eval.runner import RunConfig, new_run_dir, run_tasks
from analyst_agent.eval.schema import DEFAULT_BENCHMARK_ROOT, load_tasks

SETTINGS = EvalSettings()

app = typer.Typer(help="Benchmark evaluation harness for the analyst agent.", no_args_is_help=True)

Difficulty = Annotated[
    list[str] | None, typer.Option("--difficulty", "-d", help="L1/L2/L3/ADV; repeatable")
]
Tag = Annotated[list[str] | None, typer.Option("--tag", "-t", help="any-of match; repeatable")]
TaskId = Annotated[list[str] | None, typer.Option("--task", help="task id; repeatable")]


@app.command("list")
def list_tasks(
    difficulty: Difficulty = None,
    tag: Tag = None,
    task: TaskId = None,
    limit: Annotated[int | None, typer.Option(help="stratified sample of N tasks")] = None,
    benchmarks: Path = DEFAULT_BENCHMARK_ROOT,
) -> None:
    """List tasks matching the filters."""
    for t in load_tasks(benchmarks, difficulty, tag, task, limit):
        typer.echo(f"{t.difficulty:4} {t.id:40} {','.join(t.tags)}")


@app.command()
def run(
    difficulty: Difficulty = None,
    tag: Tag = None,
    task: TaskId = None,
    limit: Annotated[int | None, typer.Option(help="stratified sample of N tasks")] = None,
    agent: Annotated[str, typer.Option(help="core | oracle | module:callable")] = "core",
    concurrency: Annotated[int, typer.Option(min=1)] = SETTINGS.eval_concurrency,
    timeout: Annotated[
        float, typer.Option(help="per-task timeout, seconds")
    ] = SETTINGS.eval_timeout_s,
    judge_model: Annotated[
        str | None, typer.Option(help="default: AGENT_JUDGE_MODEL, else AGENT_MODEL")
    ] = SETTINGS.judge_model,
    no_judge: Annotated[
        bool, typer.Option(help="skip LLM judge (L3 fail, ADV rules only)")
    ] = False,
    no_score: bool = False,
    benchmarks: Path = DEFAULT_BENCHMARK_ROOT,
    runs_dir: Path = SETTINGS.eval_runs_dir,
) -> None:
    """Run the agent over the selected tasks and write runs/<timestamp>/."""
    tasks = load_tasks(benchmarks, difficulty, tag, task, limit)
    if not tasks:
        raise typer.BadParameter("no tasks match the filters")
    agent_fn = load_agent(agent, tasks)
    judge = (
        None
        if (no_judge or no_score)
        else LLMJudge(model=judge_model, effort=SETTINGS.judge_effort)
    )
    config = RunConfig(
        agent=agent,
        concurrency=concurrency,
        timeout_s=timeout,
        score=not no_score,
        judge_model=None if judge is None else (judge_model or "AGENT_MODEL"),
        filters={"difficulty": difficulty, "tag": tag, "task": task, "limit": limit},
    )
    run_dir = new_run_dir(runs_dir)
    typer.echo(f"running {len(tasks)} tasks -> {run_dir}")
    asyncio.run(run_tasks(tasks, agent_fn, judge=judge, config=config, run_dir=run_dir))
    md = report_run(run_dir)
    (run_dir / "report.md").write_text(md)
    typer.echo(md)


@app.command()
def report(run_dir: Path, out: Path | None = None) -> None:
    """Aggregate a run into a Markdown report."""
    md = report_run(run_dir)
    if out:
        out.write_text(md)
    typer.echo(md)


@app.command()
def compare(run_a: Path, run_b: Path, out: Path | None = None) -> None:
    """Diff two runs side by side (B relative to A)."""
    md = compare_runs(run_a, run_b)
    if out:
        out.write_text(md)
    typer.echo(md)


if __name__ == "__main__":
    app()
