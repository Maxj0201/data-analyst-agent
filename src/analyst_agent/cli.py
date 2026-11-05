"""Command-line entry point: ``analyst-agent ask --data path.csv "question"``."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Annotated

import typer

from analyst_agent.core.approval import PendingAction
from analyst_agent.tools.base import table_name_for

app = typer.Typer(add_completion=False, help="Autonomous data analyst agent.")

# Either one lets the Anthropic SDK authenticate (same pair as eval.judge.CREDENTIAL_ENV_VARS).
_CREDENTIAL_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


@app.callback()
def _main() -> None:
    """Autonomous data analyst agent."""


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="Natural-language question about the data.")],
    data: Annotated[
        list[Path],
        typer.Option(
            "--data",
            "-d",
            exists=True,
            dir_okay=False,
            help="CSV, Parquet, SQLite or DuckDB file(s); repeat for several.",
        ),
    ],
    max_turns: Annotated[int | None, typer.Option(help="Override AGENT_MAX_TURNS.")] = None,
    token_budget: Annotated[int | None, typer.Option(help="Override AGENT_TOKEN_BUDGET.")] = None,
    model: Annotated[str | None, typer.Option(help="Override AGENT_MODEL.")] = None,
    trace: Annotated[
        Path | None, typer.Option(help="Write the full run trace as JSON to this path.")
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Print each step.")] = False,
    require_approval: Annotated[
        bool | None,
        typer.Option(
            "--require-approval/--no-require-approval",
            help="Override AGENT_APPROVAL_ENABLED (human approval for costly/risky steps).",
        ),
    ] = None,
    auto_approve: Annotated[
        bool,
        typer.Option(
            "--auto-approve",
            help="Approve every gated step without prompting (otherwise ask y/n on the TTY).",
        ),
    ] = False,
    routing: Annotated[
        bool | None,
        typer.Option(
            "--routing/--no-routing",
            help="Override AGENT_ROUTING_ENABLED (pick the model per question difficulty).",
        ),
    ] = None,
    semantic_layer: Annotated[
        bool | None,
        typer.Option(
            "--semantic-layer/--no-semantic-layer",
            help="Override AGENT_SEMANTIC_LAYER_ENABLED (inject governed metric definitions).",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Use a scripted offline LLM (no API key, no cost) to exercise the full pipeline.",
        ),
    ] = False,
) -> None:
    """Answer QUESTION over the given data file(s); prints the report and a cost summary."""
    from analyst_agent import sandbox
    from analyst_agent.core import AgentLoop, LLMClient, Settings
    from analyst_agent.core.approval import Approver, AutoApprover, CallbackApprover
    from analyst_agent.core.langfuse_export import LangfuseSink, make_langfuse_client
    from analyst_agent.core.llm import estimate_cost_usd
    from analyst_agent.core.logs import configure_logging
    from analyst_agent.core.tracing import EventedLLM, SpanSink, TraceRecorder, save_trace
    from analyst_agent.tools import ToolContext, build_default_registry

    overrides = {
        k: v
        for k, v in {
            "max_turns": max_turns,
            "token_budget": token_budget,
            "model": model,
            "approval_enabled": require_approval,
            "routing_enabled": routing,
            "semantic_layer_enabled": semantic_layer,
        }.items()
        if v is not None
    }
    if not dry_run and not any(os.environ.get(v) for v in _CREDENTIAL_ENV_VARS):
        typer.secho(
            "No Anthropic credentials: set ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN), "
            "or add --dry-run to run the whole pipeline offline with a scripted LLM.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    settings = Settings(**overrides)
    # Keep stderr quiet for interactive use unless asked: --verbose, JSON logs or an explicit
    # AGENT_LOG_LEVEL turn the structured log lines (with run_id) on.
    explicit = verbose or settings.log_json_enabled or "AGENT_LOG_LEVEL" in os.environ
    configure_logging(level=settings.log_level if explicit else "WARNING")

    datasets: dict[str, Path] = {}
    for p in data:
        name = table_name_for(p)
        if name in datasets:
            raise typer.BadParameter(f"two files map to the same table name {name!r}")
        datasets[name] = p

    try:
        tables = sandbox.load_data(datasets, artifacts_dir=settings.artifacts_dir)
    except sandbox.DataLoadError as exc:
        raise typer.BadParameter(str(exc), param_hint="--data") from exc
    ctx = ToolContext(
        tables=tables,
        artifacts_dir=settings.artifacts_dir,
    )

    sinks: list[SpanSink] = []
    langfuse = make_langfuse_client() if settings.langfuse_enabled else None
    if langfuse is not None:
        sinks.append(LangfuseSink(langfuse))
    recorder = TraceRecorder(sinks=sinks, question=question)

    def on_event(kind: str, payload: dict) -> None:
        recorder.on_event(kind, payload)
        if verbose:
            typer.secho(f"[{kind}] {json.dumps(payload, default=str)[:300]}", err=True, dim=True)

    if dry_run:
        from analyst_agent.core.fake_llm import dry_run_llm

        typer.secho("Dry run: scripted offline LLM, no API calls.", err=True, dim=True)
        llm = dry_run_llm(
            question,
            tables[0],
            sandbox.run_sql,
            structured_report=settings.structured_report_enabled,
        )
    else:
        llm = LLMClient(settings)
    approver: Approver = AutoApprover() if auto_approve else CallbackApprover(prompt_approval)
    loop = AgentLoop(
        EventedLLM(llm, on_event, cost_fn=estimate_cost_usd),
        build_default_registry(),
        ctx,
        settings,
        on_event=on_event,
        approver=approver,
    )
    result = loop.run(question)
    root = recorder.finish(
        status=result.status.value,
        output=result.report,
        error=result.state.detail if not result.ok else None,
    )
    if langfuse is not None:
        langfuse.shutdown()

    typer.echo(result.report)
    if result.key_numbers:
        typer.echo("\nKey numbers: " + json.dumps(result.key_numbers, default=str))
    if result.state.artifacts:
        typer.echo("Artifacts: " + ", ".join(result.state.artifacts))
    color = typer.colors.GREEN if result.ok else typer.colors.YELLOW
    typer.secho(
        f"\nStatus: {result.status.value} | turns {result.state.turns} | "
        f"critic rounds {result.state.critic_rounds}",
        fg=color,
        err=True,
    )
    typer.secho(f"Cost: {result.cost.summary()}", err=True)
    if trace:
        doc = result.to_dict()
        doc["spans"] = root.to_dict()
        doc["trace_id"] = root.metadata.get("langfuse_trace_id")
        trace.write_text(json.dumps(doc, indent=2, default=str))
    if settings.trace_store_enabled:
        path = save_trace(settings.traces_dir, root)
        typer.secho(
            f"Trace: {path} (analyst-agent trace show {result.state.run_id})", err=True, dim=True
        )
    if not result.ok:
        raise typer.Exit(code=1)


def prompt_approval(action: PendingAction) -> tuple[bool, str | None]:
    """Interactive y/n prompt on stderr. No answer (EOF / non-interactive) means denied."""
    typer.secho("\n⚠  Approval required", fg=typer.colors.YELLOW, bold=True, err=True)
    typer.echo(f"  Step:    {action.tool}", err=True)
    for reason in action.reasons:
        typer.echo(f"  Reason:  {reason}", err=True)
    src = action.input.get("query") or action.input.get("code")
    if src:
        typer.echo("  " + "\n  ".join(str(src).splitlines()[:30]), err=True)
    try:
        approved = typer.confirm("Run this step?", default=False, err=True)
    except (typer.Abort, EOFError):
        return False, "no answer on stdin"
    if approved:
        return True, None
    try:
        note = typer.prompt("Optional note for the agent", default="", show_default=False, err=True)
    except (typer.Abort, EOFError):
        note = ""
    return False, note.strip() or None


# ---- trace inspection -----------------------------------------------------------------------

trace_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect saved run traces (AGENT_TRACE_STORE_ENABLED, ask --trace, eval runs).",
)
app.add_typer(trace_app, name="trace")

DirsOption = Annotated[
    list[Path] | None,
    typer.Option(
        "--dir",
        help="Where to look for saved traces (repeatable). "
        "Default: AGENT_TRACES_DIR and AGENT_EVAL_RUNS_DIR.",
    ),
]


def _trace_dirs(dirs: list[Path] | None) -> list[Path]:
    if dirs:
        return dirs
    from analyst_agent.core.config import Settings

    s = Settings()
    return [s.traces_dir, s.eval_runs_dir]


@trace_app.command("show")
def trace_show(
    ref: Annotated[
        str, typer.Argument(help="Run id, job id, eval task id, or a path to a trace JSON file.")
    ],
    dirs: DirsOption = None,
    compact: Annotated[
        bool, typer.Option("--compact", help="Hide zero-duration spans (plans, escalations).")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print the span tree as JSON.")] = False,
) -> None:
    """Pretty-print a saved trace as a tree with timings and costs (no Langfuse needed)."""
    from analyst_agent.core.tracing import find_trace, load_trace, render_tree

    path = find_trace(ref, _trace_dirs(dirs))
    if path is None:
        searched = ", ".join(str(d) for d in _trace_dirs(dirs))
        typer.secho(f"No saved trace for {ref!r} (searched: {searched}).", err=True, fg="red")
        raise typer.Exit(code=1)
    try:
        root = load_trace(path)
    except (ValueError, OSError) as exc:
        typer.secho(str(exc), err=True, fg="red")
        raise typer.Exit(code=1) from exc
    if as_json:
        typer.echo(json.dumps(root.to_dict(), indent=2, default=str))
        return
    typer.secho(str(path), err=True, dim=True)
    typer.echo(render_tree(root, show_instant=not compact, color=sys.stdout.isatty()))


@trace_app.command("ls")
def trace_ls(
    dirs: DirsOption = None,
    limit: Annotated[int, typer.Option(help="Show at most this many (newest first).")] = 20,
) -> None:
    """List saved traces: id, status, duration, cost."""
    from analyst_agent.core.tracing import load_trace

    files = [f for d in _trace_dirs(dirs) if d.is_dir() for f in d.rglob("*.json")]
    rows = []
    for f in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            root = load_trace(f)
        except (ValueError, OSError):
            continue
        md = root.metadata
        ident = md.get("run_id") or md.get("job_id") or f.stem
        dur = f"{root.duration:.1f}s" if root.duration is not None else "-"
        rows.append(
            f"{ident:24} {str(md.get('status') or '-'):22} {dur:>8} ${root.total_cost():.4f}  {f}"
        )
        if len(rows) >= limit:
            break
    typer.echo("\n".join(rows) if rows else "No saved traces.")


if __name__ == "__main__":  # pragma: no cover
    app()
