"""Tool definitions (JSON schemas) and their Python handlers."""

from __future__ import annotations

from typing import Any

from analyst_agent.sandbox import quote_ident
from analyst_agent.tools.base import Tool, ToolContext, ToolResult
from analyst_agent.tools.formatting import rows_to_markdown
from analyst_agent.tools.make_chart import CHART_TYPES, MAKE_CHART, make_chart

__all__ = [
    "ALL_TOOLS",
    "CHART_TYPES",
    "FINAL_REPORT",
    "INSPECT_SCHEMA",
    "MAKE_CHART",
    "RUN_PYTHON",
    "RUN_SQL",
    "final_report",
    "inspect_schema",
    "make_chart",
    "run_python",
    "run_sql",
]

# --------------------------------------------------------------------------- inspect_schema


def _describe_table(table: str, ctx: ToolContext) -> tuple[str, dict[str, Any]]:
    lit = table.replace("'", "''")
    cols = ctx.run_sql(
        "SELECT column_name, data_type FROM information_schema.columns "
        f"WHERE table_name = '{lit}' ORDER BY ordinal_position"
    )
    if not cols.ok:
        raise RuntimeError(cols.error or "failed to read columns")
    columns = [(r["column_name"], r["data_type"]) for r in cols.rows]
    if not columns:
        raise RuntimeError(f"table {table!r} has no columns or does not exist")

    counts_sql = ", ".join(
        ["COUNT(*) AS __n"]
        + [f"COUNT({quote_ident(c)}) AS c{i}" for i, (c, _) in enumerate(columns)]
    )
    counts = ctx.run_sql(f"SELECT {counts_sql} FROM {quote_ident(table)}")
    if not counts.ok:
        raise RuntimeError(counts.error or "failed to count rows")
    crow = counts.rows[0]
    n = int(crow["__n"])
    null_rates = {
        c: (round(1 - int(crow[f"c{i}"]) / n, 4) if n else 0.0) for i, (c, _) in enumerate(columns)
    }

    sample = ctx.run_sql(f"SELECT * FROM {quote_ident(table)} LIMIT 5")
    sample_rows = sample.rows if sample.ok else []

    info = {
        "table": table,
        "row_count": n,
        "columns": [{"name": c, "dtype": t, "null_rate": null_rates[c]} for c, t in columns],
        "sample_rows": sample_rows,
    }
    lines = [
        f"### Table `{table}` — {n:,} rows",
        "",
        "| column | dtype | null_rate |",
        "|---|---|---|",
    ]
    lines += [f"| {c} | {t} | {null_rates[c]:.2%} |" for c, t in columns]
    lines += ["", "Sample rows:", rows_to_markdown([c for c, _ in columns], sample_rows)]
    return "\n".join(lines), info


def inspect_schema(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    table = args.get("table")
    if table is not None and table not in ctx.tables:
        return ToolResult(
            f"Unknown table {table!r}. Available tables: {', '.join(ctx.tables)}", is_error=True
        )
    targets = [table] if table else list(ctx.tables)
    parts, infos = [f"Available tables: {', '.join(ctx.tables)}"], []
    for t in targets:
        text, info = _describe_table(t, ctx)
        parts.append(text)
        infos.append(info)
    return ToolResult("\n\n".join(parts), data={"tables": infos})


INSPECT_SCHEMA = Tool(
    name="inspect_schema",
    description=(
        "Describe the dataset: column names, dtypes, row count, null rate per column and 5 "
        "sample rows. Omit `table` to describe every table."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "table": {"type": "string", "description": "Table name. Omit for all tables."}
        },
        "additionalProperties": False,
    },
    handler=inspect_schema,
)

# --------------------------------------------------------------------------- run_sql


def run_sql(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Model sees ``markdown_preview`` on success, ``error`` on failure."""
    query = args["query"]
    res = ctx.run_sql(query)
    if not res.ok:
        return ToolResult(f"SQL error: {res.error}", is_error=True, data={"query": query})
    return ToolResult(
        res.markdown_preview,
        data={
            "query": query,
            "columns": res.columns,
            "rows": res.rows[:50],
            "row_count": res.row_count,
            "truncated": res.truncated,
            "duration_ms": res.duration_ms,
        },
    )


RUN_SQL = Tool(
    name="run_sql",
    description=(
        "Run one read-only SQL query (DuckDB dialect; SELECT/WITH/EXPLAIN only) against the "
        "loaded tables. Returns up to 200 rows plus the total row count."
    ),
    input_schema={
        "type": "object",
        "properties": {"query": {"type": "string", "description": "A single SQL statement."}},
        "required": ["query"],
        "additionalProperties": False,
    },
    handler=run_sql,
)

# --------------------------------------------------------------------------- run_python


def _format_python_output(stdout: str, artifacts: list[str]) -> str:
    body = f"stdout:\n{stdout or '(empty)'}"
    if artifacts:
        body += "\nartifacts:\n" + "\n".join(f"- {a}" for a in artifacts)
    return body


def run_python(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Model sees stdout + artifact paths on success; ``error`` (then any stdout) on failure."""
    code = args["code"]
    res = ctx.run_python(code)
    data = {"code": code, "artifacts": list(res.artifacts), "duration_ms": res.duration_ms}
    if not res.ok:
        content = f"Python error: {res.error}"
        if res.stdout:
            content += "\n" + _format_python_output(res.stdout, res.artifacts)
        return ToolResult(content, is_error=True, data=data)
    data["stdout"] = res.stdout[:5000]
    return ToolResult(_format_python_output(res.stdout, res.artifacts), data=data)


RUN_PYTHON = Tool(
    name="run_python",
    description=(
        "Run a Python snippet in a sandbox with pandas and numpy. Tables are preloaded as "
        "`tables[name]` (DataFrames); `df` is the table when there is exactly one. Print what you "
        "need to see: stdout is returned, plus the paths of any files the code writes to its "
        "working directory (e.g. plt.savefig('fig.png')). No network access."
    ),
    input_schema={
        "type": "object",
        "properties": {"code": {"type": "string", "description": "Python source code."}},
        "required": ["code"],
        "additionalProperties": False,
    },
    handler=run_python,
)

# --------------------------------------------------------------------------- final_report


def final_report(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Accepts free Markdown (``markdown``) or the structured fields (``headline``, ``method``,
    ``caveats``, ``charts``; see :mod:`analyst_agent.core.report`), which are validated and
    rendered to Markdown. The loop requires the structured form when
    ``AGENT_STRUCTURED_REPORT_ENABLED`` is on."""
    from analyst_agent.core.report import (
        ReportValidationError,
        is_structured,
        parse_structured_report,
    )

    key_numbers = args.get("key_numbers") or {}
    if not isinstance(key_numbers, dict):
        return ToolResult("`key_numbers` must be an object of name -> number.", is_error=True)
    if is_structured(args):
        try:
            report = parse_structured_report(args)
        except ReportValidationError as exc:
            problems = "\n".join(f"- {e}" for e in exc.errors)
            return ToolResult(f"Invalid structured report:\n{problems}", is_error=True)
        return ToolResult(
            "Report submitted for review.",
            data={
                "markdown": report.to_markdown(),
                "key_numbers": report.key_numbers,
                "structured": report.to_dict(),
            },
            terminate=True,
        )
    markdown = args.get("markdown", "")
    if not isinstance(markdown, str) or not markdown.strip():
        return ToolResult("`markdown` must not be empty.", is_error=True)
    return ToolResult(
        "Report submitted for review.",
        data={"markdown": markdown, "key_numbers": key_numbers},
        terminate=True,
    )


FINAL_REPORT = Tool(
    name="final_report",
    description=(
        "Submit the final Markdown report and the key numbers that answer the question. Call once "
        "when the analysis is complete and verified; this ends the analysis (a reviewer may send "
        "it back with feedback)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "markdown": {"type": "string", "description": "The full report in Markdown."},
            "key_numbers": {
                "type": "object",
                "description": 'Metric name -> numeric value, e.g. {"total_revenue": 12345.6}.',
                "additionalProperties": {"type": ["number", "string", "null"]},
            },
        },
        # ``markdown`` is enforced by the handler so the structured form (core/report.py), which
        # the loop advertises instead when AGENT_STRUCTURED_REPORT_ENABLED is on, can omit it.
        "required": ["key_numbers"],
        "additionalProperties": False,
    },
    handler=final_report,
)

ALL_TOOLS = (INSPECT_SCHEMA, RUN_SQL, RUN_PYTHON, MAKE_CHART, FINAL_REPORT)
