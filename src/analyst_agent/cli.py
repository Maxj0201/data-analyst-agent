"""Command-line entry point: ``analyst-agent ask --data path.csv "question"``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

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
) -> None:
    """Answer QUESTION over the given data file(s); prints the report and a cost summary."""
    from analyst_agent.core import AgentLoop, LLMClient, Settings
    from analyst_agent.tools import ToolContext, build_default_registry, create_sandbox
    from analyst_agent.tools.sandbox_iface import table_name_for

    overrides = {
        k: v
        for k, v in {"max_turns": max_turns, "token_budget": token_budget, "model": model}.items()
        if v is not None
    }
    settings = Settings(**overrides)

    datasets: dict[str, Path] = {}
    for p in data:
        name = table_name_for(p)
        if name in datasets:
            raise typer.BadParameter(f"two files map to the same table name {name!r}")
        datasets[name] = p

    ctx = ToolContext(
        sandbox=create_sandbox(datasets),
        tables=list(datasets),
        artifacts_dir=settings.artifacts_dir,
    )

    def on_event(kind: str, payload: dict) -> None:
        if verbose:
            typer.secho(f"[{kind}] {json.dumps(payload, default=str)[:300]}", err=True, dim=True)

    loop = AgentLoop(
        LLMClient(settings), build_default_registry(), ctx, settings, on_event=on_event
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


if __name__ == "__main__":  # pragma: no cover
    app()
