"""Tool definitions (JSON schemas) and their Python handlers."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from analyst_agent import sandbox
from analyst_agent.sandbox import SqlResult
from analyst_agent.tools.base import Tool, ToolContext, ToolResult
from analyst_agent.tools.formatting import rows_to_markdown

# --------------------------------------------------------------------------- inspect_schema


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _describe_table(table: str, ctx: ToolContext) -> tuple[str, dict[str, Any]]:
    lit = table.replace("'", "''")
    cols = sandbox.run_sql(
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
        + [f"COUNT({_quote_ident(c)}) AS c{i}" for i, (c, _) in enumerate(columns)]
    )
    counts = sandbox.run_sql(f"SELECT {counts_sql} FROM {_quote_ident(table)}")
    if not counts.ok:
        raise RuntimeError(counts.error or "failed to count rows")
    crow = counts.rows[0]
    n = int(crow["__n"])
    null_rates = {
        c: (round(1 - int(crow[f"c{i}"]) / n, 4) if n else 0.0) for i, (c, _) in enumerate(columns)
    }

    sample = sandbox.run_sql(f"SELECT * FROM {_quote_ident(table)} LIMIT 5")
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


def _format_sql_result(res: SqlResult) -> str:
    head = f"{res.total_rows:,} row(s)"
    if res.truncated:
        head += f" (showing first {len(res.rows)})"
    return f"{head}\n{res.preview_markdown or rows_to_markdown(res.columns, res.rows)}"


def run_sql(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    query = args["query"]
    res = sandbox.run_sql(query)
    if not res.ok:
        return ToolResult(f"SQL error: {res.error}", is_error=True, data={"query": query})
    return ToolResult(
        _format_sql_result(res),
        data={
            "query": query,
            "columns": res.columns,
            "rows": res.rows[:50],
            "total_rows": res.total_rows,
            "truncated": res.truncated,
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


def run_python(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    code = args["code"]
    res = sandbox.run_python(code)
    body = f"stdout:\n{res.stdout or '(empty)'}"
    if res.stderr:
        body += f"\nstderr:\n{res.stderr[-2000:]}"
    if not res.ok:
        return ToolResult(f"Python error: {res.error}\n{body}", is_error=True, data={"code": code})
    return ToolResult(body, data={"code": code, "stdout": res.stdout[:5000]})


RUN_PYTHON = Tool(
    name="run_python",
    description=(
        "Run a Python snippet in a sandbox with pandas and numpy. Tables are preloaded as "
        "`tables[name]` (DataFrames); `df` is the table when there is exactly one. Print what you "
        "need to see; only stdout/stderr are returned. No network or file access."
    ),
    input_schema={
        "type": "object",
        "properties": {"code": {"type": "string", "description": "Python source code."}},
        "required": ["code"],
        "additionalProperties": False,
    },
    handler=run_python,
)

# --------------------------------------------------------------------------- make_chart

CHART_TYPES = ("bar", "barh", "line", "scatter", "hist", "pie")


def make_chart(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    spec = args["spec"]
    kind = spec.get("chart_type", "bar")
    if kind not in CHART_TYPES:
        return ToolResult(f"Unsupported chart_type {kind!r}; use one of {CHART_TYPES}", True)
    series = spec.get("series") or [
        {"name": spec.get("y_label", ""), "x": spec.get("x"), "y": spec.get("y")}
    ]
    for s in series:
        if s.get("y") is None or (kind not in ("hist", "pie") and s.get("x") is None):
            return ToolResult(
                "Each series needs `y` (and `x` unless chart_type is hist/pie).", True
            )
        if s.get("x") is not None and len(s["x"]) != len(s["y"]):
            return ToolResult("Series `x` and `y` must have the same length.", True)

    fig, ax = plt.subplots(figsize=(8, 5), dpi=120)
    try:
        for s in series:
            label = s.get("name") or None
            if kind == "bar":
                ax.bar([str(v) for v in s["x"]], s["y"], label=label)
            elif kind == "barh":
                ax.barh([str(v) for v in s["x"]], s["y"], label=label)
            elif kind == "line":
                ax.plot(s["x"], s["y"], marker="o", label=label)
            elif kind == "scatter":
                ax.scatter(s["x"], s["y"], label=label)
            elif kind == "hist":
                ax.hist(s["y"], bins=spec.get("bins", 20), label=label)
            elif kind == "pie":
                ax.pie(
                    s["y"], labels=[str(v) for v in (s.get("x") or [])] or None, autopct="%1.1f%%"
                )
        ax.set_title(spec.get("title", ""))
        if kind != "pie":
            ax.set_xlabel(spec.get("x_label", ""))
            ax.set_ylabel(spec.get("y_label", ""))
        if len(series) > 1:
            ax.legend()
        if kind == "bar" and len(series[0]["x"]) > 6:
            plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
        fig.tight_layout()

        ctx.artifacts_dir.mkdir(parents=True, exist_ok=True)
        slug = (
            re.sub(r"[^a-z0-9]+", "-", spec.get("title", "chart").lower()).strip("-")[:40]
            or "chart"
        )
        digest = hashlib.sha1(json.dumps(spec, sort_keys=True, default=str).encode()).hexdigest()[
            :8
        ]
        path = ctx.artifacts_dir / f"{slug}-{digest}.png"
        fig.savefig(path)
    finally:
        plt.close(fig)
    return ToolResult(f"Chart saved to {path}", data={"path": str(path)})


_SERIES_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "x": {"type": "array", "items": {}},
        "y": {"type": "array", "items": {"type": "number"}},
    },
    "required": ["y"],
}

MAKE_CHART = Tool(
    name="make_chart",
    description=(
        "Render a matplotlib chart from data you already computed and save it as a PNG under "
        "artifacts/. Returns the file path; reference it in the report as ![title](path)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "spec": {
                "type": "object",
                "properties": {
                    "chart_type": {"type": "string", "enum": list(CHART_TYPES)},
                    "title": {"type": "string"},
                    "x_label": {"type": "string"},
                    "y_label": {"type": "string"},
                    "x": {"type": "array", "items": {}, "description": "Single-series x values."},
                    "y": {"type": "array", "items": {"type": "number"}},
                    "series": {"type": "array", "items": _SERIES_SCHEMA},
                    "bins": {"type": "integer"},
                },
                "required": ["chart_type", "title"],
            }
        },
        "required": ["spec"],
        "additionalProperties": False,
    },
    handler=make_chart,
)

# --------------------------------------------------------------------------- final_report


def final_report(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    markdown = args.get("markdown", "")
    key_numbers = args.get("key_numbers") or {}
    if not isinstance(key_numbers, dict):
        return ToolResult("`key_numbers` must be an object of name -> number.", is_error=True)
    if not markdown.strip():
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
        "required": ["markdown", "key_numbers"],
        "additionalProperties": False,
    },
    handler=final_report,
)

ALL_TOOLS = (INSPECT_SCHEMA, RUN_SQL, RUN_PYTHON, MAKE_CHART, FINAL_REPORT)
