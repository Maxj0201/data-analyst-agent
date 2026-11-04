"""Command-line entry point: ``analyst-agent ask --data path.csv "question"``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from analyst_agent.core.approval import PendingAction
from analyst_agent.tools.base import table_name_for

app = typer.Typer(add_completion=False, help="Autonomous data analyst agent.")


@app.callback()
def _main() -> None:
    """Autonomous data analyst agent."""


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="Natural-language question about the data.")],
    data: Annotated[
        list[Path],
        typer.Option("--data", "-d", exists=True, dir_okay=False, help="CSV/Parquet file(s)."),
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
    from analyst_agent.tools import ToolContext, build_default_registry

    overrides = {
        k: v
        for k, v in {
            "max_turns": max_turns,
            "token_budget": token_budget,
            "model": model,
            "approval_enabled": require_approval,
        }.items()
        if v is not None
    }
    settings = Settings(**overrides)

    datasets: dict[str, Path] = {}
    for p in data:
        name = table_name_for(p)
        if name in datasets:
            raise typer.BadParameter(f"two files map to the same table name {name!r}")
        datasets[name] = p

    tables = sandbox.load_data(datasets, artifacts_dir=settings.artifacts_dir)
    ctx = ToolContext(
        tables=tables,
        artifacts_dir=settings.artifacts_dir,
    )

    def on_event(kind: str, payload: dict) -> None:
        if verbose:
            typer.secho(f"[{kind}] {json.dumps(payload, default=str)[:300]}", err=True, dim=True)

    if dry_run:
        from analyst_agent.core.fake_llm import dry_run_llm

        typer.secho("Dry run: scripted offline LLM, no API calls.", err=True, dim=True)
        llm = dry_run_llm(question, tables[0], sandbox.run_sql)
    else:
        llm = LLMClient(settings)
    approver: Approver = AutoApprover() if auto_approve else CallbackApprover(prompt_approval)
    loop = AgentLoop(
        llm, build_default_registry(), ctx, settings, on_event=on_event, approver=approver
    )
    result = loop.run(question)

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
        trace.write_text(json.dumps(result.to_dict(), indent=2, default=str))
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


if __name__ == "__main__":  # pragma: no cover
    app()
