"""Eval CLI: ``python -m analyst_agent.eval {run,report,compare,list}``."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any

import typer

from analyst_agent.core.config import Settings
from analyst_agent.eval.agent import load_agent
from analyst_agent.eval.judge import BatchJudge, LLMJudge, judge_configured
from analyst_agent.eval.report import compare_runs, report_run, summarize
from analyst_agent.eval.runner import (
    ResumeError,
    RunConfig,
    load_results,
    new_run_dir,
    run_tasks,
)
from analyst_agent.eval.schema import (
    DEFAULT_BENCHMARK_ROOT,
    DESCRIPTOR_TAGS,
    DOMAIN_TAGS,
    TAXONOMY,
    Task,
    load_tasks,
)

SETTINGS = Settings()

app = typer.Typer(help="Benchmark evaluation harness for the analyst agent.", no_args_is_help=True)

Difficulty = Annotated[
    list[str] | None, typer.Option("--difficulty", "-d", help="L1/L2/L3/ADV; repeatable")
]
Tag = Annotated[
    list[str] | None,
    typer.Option("--tag", "-t", help="task tag (see `eval tags`); repeatable, any-of match"),
]
AllTags = Annotated[
    bool, typer.Option("--all-tags", help="require every --tag instead of any of them")
]
TaskId = Annotated[list[str] | None, typer.Option("--task", help="task id; repeatable")]


def _select(
    benchmarks: Path,
    difficulty: list[str] | None,
    tag: list[str] | None,
    task: list[str] | None,
    limit: int | None,
    all_tags: bool,
) -> list[Task]:
    try:
        return load_tasks(benchmarks, difficulty, tag, task, limit, match_all_tags=all_tags)
    except ValueError as e:
        raise typer.BadParameter(str(e), param_hint="--tag") from e


@app.command("list")
def list_tasks(
    difficulty: Difficulty = None,
    tag: Tag = None,
    task: TaskId = None,
    limit: Annotated[int | None, typer.Option(help="stratified sample of N tasks")] = None,
    all_tags: AllTags = False,
    benchmarks: Path = DEFAULT_BENCHMARK_ROOT,
) -> None:
    """List tasks matching the filters."""
    for t in _select(benchmarks, difficulty, tag, task, limit, all_tags):
        typer.echo(f"{t.difficulty:4} {t.id:40} {','.join(t.tags)}")


@app.command("tags")
def list_tags(benchmarks: Path = DEFAULT_BENCHMARK_ROOT) -> None:
    """Show the tag vocabulary with task counts per difficulty."""
    tasks = load_tasks(benchmarks)
    levels = ("L1", "L2", "L3", "ADV")
    typer.echo(f"{'tag':16} {'total':>5} " + " ".join(f"{d:>4}" for d in levels))
    for title, group in (
        ("taxonomy", TAXONOMY),
        ("domain", DOMAIN_TAGS),
        ("descriptor", DESCRIPTOR_TAGS),
    ):
        typer.echo(f"-- {title}")
        for tag in group:
            hits = [t for t in tasks if tag in t.tags]
            per = " ".join(f"{sum(t.difficulty == d for t in hits):>4}" for d in levels)
            typer.echo(f"{tag:16} {len(hits):>5} {per}")


@app.command()
def run(
    difficulty: Difficulty = None,
    tag: Tag = None,
    task: TaskId = None,
    limit: Annotated[int | None, typer.Option(help="stratified sample of N tasks")] = None,
    all_tags: AllTags = False,
    agent: Annotated[str, typer.Option(help="core | dry-run | oracle | module:callable")] = "core",
    concurrency: Annotated[int, typer.Option(min=1)] = SETTINGS.eval_concurrency,
    timeout: Annotated[
        float, typer.Option(help="per-task timeout, seconds")
    ] = SETTINGS.eval_timeout_s,
    judge_model: Annotated[
        str | None, typer.Option(help="default: AGENT_JUDGE_MODEL, else AGENT_MODEL")
    ] = SETTINGS.judge_model,
    no_judge: Annotated[
        bool,
        typer.Option(
            help="skip the LLM judge (L3 tasks skipped, ADV rules only); implied when no "
            "ANTHROPIC_API_KEY is set"
        ),
    ] = False,
    no_score: bool = False,
    isolation: Annotated[
        str,
        typer.Option(help="process: each task in a child process killed on timeout | inline"),
    ] = "process",
    fail_under: Annotated[
        float | None,
        typer.Option(help="exit 1 if accuracy over scored tasks is below this (0-1)"),
    ] = None,
    fail_on_error: Annotated[
        bool,
        typer.Option(
            help="exit 1 if any task errored, timed out, or the agent stopped early "
            "(max_turns / budget_exceeded)"
        ),
    ] = False,
    routing: Annotated[
        bool | None,
        typer.Option(
            "--routing/--no-routing",
            help="force model routing on/off for the core agent (default: AGENT_ROUTING_ENABLED)",
        ),
    ] = None,
    semantic_layer: Annotated[
        bool | None,
        typer.Option(
            "--semantic-layer/--no-semantic-layer",
            help="force the semantic layer on/off for the core agent "
            "(default: AGENT_SEMANTIC_LAYER_ENABLED)",
        ),
    ] = None,
    auto_approve: Annotated[
        bool,
        typer.Option(
            "--auto-approve/--no-auto-approve",
            help="approve every human-in-the-loop gate when AGENT_APPROVAL_ENABLED is on "
            "(eval has no human; --no-auto-approve leaves no approver, so gated steps are denied)",
        ),
    ] = True,
    budget_usd: Annotated[
        float | None,
        typer.Option(
            min=0,
            help="stop starting new tasks once agent + judge spend (USD, from core.llm's price "
            "table) reaches this; unstarted tasks are recorded as over_budget and not scored. "
            "Running tasks finish, so the run can overshoot by up to --concurrency tasks",
        ),
    ] = None,
    batch: Annotated[
        bool,
        typer.Option(
            "--batch/--no-batch",
            help="send the run's judge calls as one Message Batch (half price; verdicts arrive "
            "once the last agent finishes). Falls back to direct calls when the SDK cannot "
            "batch or the batch fails [default: AGENT_JUDGE_BATCH_ENABLED]",
        ),
    ] = SETTINGS.judge_batch_enabled,
    resume: Annotated[
        Path | None,
        typer.Option(
            help="continue an interrupted run in this directory: tasks already scored are "
            "skipped, unstarted (crash, --budget-usd) tasks run, and finished tasks still "
            "missing a judge verdict are re-graded without re-running the agent. Reuses the "
            "run's task list, agent and options; pass --budget-usd to raise its cap",
        ),
    ] = None,
    retry_errors: Annotated[
        bool,
        typer.Option(help="with --resume: also re-run tasks that errored or timed out"),
    ] = False,
    benchmarks: Path = DEFAULT_BENCHMARK_ROOT,
    runs_dir: Path = SETTINGS.eval_runs_dir,
) -> None:
    """Run the agent over the selected tasks and write runs/<timestamp>/."""
    import os

    from analyst_agent.core.logs import configure_logging

    # Progress goes to stdout; structured logs (with task_id / run_id) only when asked for.
    explicit = SETTINGS.log_json_enabled or "AGENT_LOG_LEVEL" in os.environ
    configure_logging(level=SETTINGS.log_level if explicit else "WARNING")
    if isolation not in ("process", "inline"):
        raise typer.BadParameter("--isolation must be 'process' or 'inline'")
    if retry_errors and resume is None:
        raise typer.BadParameter("--retry-errors only applies with --resume")
    if resume is not None:
        if difficulty or tag or task or limit is not None or all_tags:
            raise typer.BadParameter(
                "--resume reuses the run's task list; drop the task filters", param_hint="--resume"
            )
        prev = _resume_options(resume)
        cfg = prev["config"]
        agent = cfg.get("agent", agent)
        timeout = cfg.get("timeout_s", timeout)
        isolation = cfg.get("isolation", isolation)
        no_score = not cfg.get("score", True)
        no_judge = no_judge or cfg.get("judge_model") is None
        judge_model = (
            None if cfg.get("judge_model") in (None, "AGENT_MODEL") else cfg["judge_model"]
        )
        routing = cfg.get("routing_enabled", routing)
        semantic_layer = cfg.get("semantic_layer_enabled", semantic_layer)
        auto_approve = cfg.get("auto_approve", auto_approve)
        if budget_usd is None:
            budget_usd = cfg.get("budget_usd")
        difficulty, tag, task, limit, all_tags = (
            (cfg.get("filters") or {}).get(k)
            for k in ("difficulty", "tag", "task", "limit", "all_tags")
        )
        all_tags = bool(all_tags)
        tasks = load_tasks(benchmarks, ids=prev["task_ids"])
    else:
        tasks = _select(benchmarks, difficulty, tag, task, limit, all_tags)
    if not tasks:
        raise typer.BadParameter("no tasks match the filters")
    agent_fn = load_agent(
        agent,
        tasks,
        routing_enabled=routing,
        auto_approve=auto_approve,
        semantic_layer_enabled=semantic_layer,
    )
    if not (no_judge or no_score) and not judge_configured():
        typer.secho(
            "no judge configured (ANTHROPIC_API_KEY is not set): L3 tasks are skipped and ADV "
            "tasks are scored by rules only",
            fg=typer.colors.YELLOW,
            err=True,
        )
        no_judge = True
    judge: LLMJudge | BatchJudge | None = (
        None
        if (no_judge or no_score)
        else LLMJudge(model=judge_model, effort=SETTINGS.judge_effort)
    )
    if batch and isinstance(judge, LLMJudge):
        judge = BatchJudge(judge, poll_interval_s=SETTINGS.judge_batch_poll_s)
    config = RunConfig(
        agent=agent,
        concurrency=concurrency,
        timeout_s=timeout,
        isolation=isolation,  # type: ignore[arg-type]
        score=not no_score,
        judge_model=None if judge is None else (judge_model or "AGENT_MODEL"),
        routing_enabled=routing,
        semantic_layer_enabled=semantic_layer,
        auto_approve=auto_approve,
        budget_usd=budget_usd,
        batch_judge=isinstance(judge, BatchJudge),
        filters={
            "difficulty": difficulty,
            "tag": tag,
            "all_tags": all_tags,
            "task": task,
            "limit": limit,
        },
    )
    run_dir = resume if resume is not None else new_run_dir(runs_dir)
    typer.echo(f"{'resuming' if resume else 'running'} {len(tasks)} tasks -> {run_dir}")
    try:
        asyncio.run(
            run_tasks(
                tasks,
                agent_fn,
                judge=judge,
                config=config,
                run_dir=run_dir,
                resume=resume is not None,
                retry_errors=retry_errors,
            )
        )
    except ResumeError as e:
        typer.secho(f"cannot resume: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from e
    md = report_run(run_dir)
    (run_dir / "report.md").write_text(md)
    typer.echo(md)
    _gate(run_dir, fail_under, fail_on_error)


def _resume_options(run_dir: Path) -> dict[str, Any]:
    """``meta.json`` of a run to resume (its config and task ids), or a CLI error."""
    meta_path = run_dir / "meta.json"
    if not meta_path.is_file():
        raise typer.BadParameter(f"{run_dir} has no meta.json", param_hint="--resume")
    meta = json.loads(meta_path.read_text())
    if not meta.get("task_ids"):
        raise typer.BadParameter(f"{meta_path} records no task_ids", param_hint="--resume")
    return {"config": meta.get("config") or {}, "task_ids": meta["task_ids"]}


INCOMPLETE_AGENT_STATUSES = {"max_turns", "budget_exceeded", "error"}


def _gate(run_dir: Path, fail_under: float | None, fail_on_error: bool) -> None:
    """Exit non-zero when the run misses the requested bar (used by CI)."""
    results = load_results(run_dir)
    s = summarize(results)
    problems = []
    if fail_on_error:
        bad = [
            f"{r['task_id']} ({r['status'] if r['status'] != 'ok' else r.get('agent_status')})"
            for r in results
            if r["status"] != "ok" or r.get("agent_status") in INCOMPLETE_AGENT_STATUSES
        ]
        if bad:
            problems.append("did not complete: " + ", ".join(bad))
    if fail_under is not None and (s["accuracy"] is None or s["accuracy"] < fail_under):
        acc = "n/a" if s["accuracy"] is None else f"{s['accuracy']:.1%}"
        problems.append(f"accuracy {acc} < {fail_under:.1%}")
    if problems:
        typer.secho("FAILED: " + "; ".join(problems), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


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
