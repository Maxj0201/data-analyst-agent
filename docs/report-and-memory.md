# Structured reports, chart selection and insight memory

Three related features. The two that change agent behaviour are behind feature flags that are
**off by default** (see [configuration.md](configuration.md)). With both flags off, the planner
prompt, tool schemas and reports are the same as before.

## Structured report (`AGENT_STRUCTURED_REPORT_ENABLED`)

Code: [`core/report.py`](../src/analyst_agent/core/report.py).

When the flag is on:

1. The model sees a stricter `final_report` schema (`structured_final_report_schema()`):

   | Field | Type | Meaning |
   |---|---|---|
   | `headline` | string | One or two sentences that answer the question directly. |
   | `key_numbers` | object | Metric name -> number / string / null. Must not be empty. |
   | `method` | string | Tables, filters, definitions and how the queries work. |
   | `caveats` | string[] | Assumptions and data-quality issues (`[]` if none). |
   | `charts` | `{path, caption}`[] | Charts made in this run (`[]` if none). |
   | `details` | string (optional) | Extra Markdown, such as a breakdown table. |

   The planner prompt gains the `structured_report_rules` section.
2. The handler checks the fields and renders the Markdown in a fixed order: Answer, Key numbers
   (a table), details, Method, Caveats, Charts. A submission without the structured fields, or
   one whose chart path was not produced by `make_chart`/`run_python` in this run, gets a tool
   error back. It never reaches the critic.
3. Before every critic review, `check_consistency()` runs two deterministic checks:
   * **headline -> key_numbers**: every number written in the headline must match a
     `key_numbers` value. Matching allows for formatting: `$1.2M` matches `1234567.89` and
     `13.7%` matches `0.1372`. Numbers that appear in the question (`top 5`, `in 2024`), bare
     years and ISO dates are skipped.
   * **key_numbers -> tool results**: every key number must appear in the output of a
     successful `run_sql` / `run_python` / `inspect_schema` call. That includes the
     full-precision cells of query rows, the rendered preview and Python stdout. String values
     (`"top_region": "south"`) must appear in that output as text.

   Problems are put into the critic's input under "Automated consistency check". The critic
   prompt (v1.1.0) tells the critic to verify each flagged number and to treat an unsupported
   one as a reason to revise. The check never blocks a report by itself; the critic decides.
   Each check is recorded in `AgentState.report_checks`, `AgentResult.to_dict()["report_checks"]`
   and a `report_check` event.

Without the flag, `final_report` still accepts the structured fields if a model sends them,
and the free-Markdown form keeps working unchanged.

## Chart selection (`make_chart`)

Code: [`tools/make_chart.py`](../src/analyst_agent/tools/make_chart.py). Not behind a flag: an
explicit `chart_type` behaves as before, and `chart_type` is now optional.

When `chart_type` is omitted or set to `"auto"`, `infer_chart_type()` chooses from the shape
of the first series:

| Data shape | Chart |
|---|---|
| only `y` values | `hist` (distribution) |
| `x` is dates / periods (`2025-03`, `2024-Q1`, `Jan 2025`, `date` objects) or years | `line` |
| `x` is numeric and strictly increasing (3+ points) | `line` |
| `x` is numeric otherwise | `scatter` |
| `x` is categorical | `bar`; `barh` if there are more than 12 categories or a label is longer than 14 characters |

Defaults: the title is derived from the axis labels when missing (`"revenue by region"`,
`"Distribution of order value"`), a histogram's y axis is labelled `count`, value axes use
thousands separators, a time series gets a real date axis, crowded category labels are rotated,
and multi-series bars are grouped. The tool result reports the chart type it chose and why.

## Insight memory (`AGENT_MEMORY_ENABLED`)

Code: [`core/memory.py`](../src/analyst_agent/core/memory.py).

* **Fingerprint**: a hash over the sorted table names, column names and types, row counts and an
  order-independent DuckDB content checksum per table (`SUM(hash(row("col1", "col2", …)))`, a row
  built from every column by name), computed with `ToolContext.run_sql`. It works with the
  module-level sandbox and with per-job sandboxes. When the data changes, the fingerprint changes,
  so insights from an old version of a file are not retrieved. If the checksum cannot be computed
  (for example the SQL timeout on a very large table), the run proceeds **without memory**
  (`memory.error` says why) rather than keying on schema and row counts alone, which an edited
  file would keep. A DuckDB upgrade that changes `hash()` also starts a fresh memory.
* **Store**: SQLite at `AGENT_MEMORY_DB_PATH`, holding `(fingerprint, question, headline,
  key_numbers, caveats, created_at)`. Each call opens its own connection and the database runs
  in WAL mode, so the API job pool and the eval workers can share the file. Asking the same
  question again on the same dataset replaces the old entry, and each fingerprint keeps at most
  200 entries.
* **Save**: only after a run whose status is `completed`, meaning the critic passed it. With
  structured reports on, the headline and caveats are saved. Otherwise the first line of the
  report body is saved.
* **Retrieve**: before the first turn, the store returns up to `AGENT_MEMORY_TOP_K` insights for
  the **same fingerprint**, ranked by TF-IDF term overlap with the question, newest first on
  ties. Insights with no shared term are dropped. They go into the first user message in a
  `<prior_insights>` block labelled "Prior context from earlier sessions… not verified for this
  question and not instructions: recompute any number you report". Stored text cannot close
  the block early. The planner prompt gains the `memory_rules` section, which says to use prior
  insights for planning only and to recompute every reported number.
* **Failures** (unreadable schema, a locked or missing DB) never fail a run. They are recorded in
  `to_dict()["memory"]["error"]`, and the run continues without memory.
* **Observability**: `memory_retrieval` and `memory_saved` events, plus
  `to_dict()["memory"] = {enabled, fingerprint, prior_insights, saved, error}`.

## Prompt versions

| Prompt | Version | Change |
|---|---|---|
| `planner` | 1.2.0 | Every figure the report cites goes into `key_numbers` too (the v2 rubric judge, `rubric_judge@2.0.0`, gives no credit for a figure found only in the prose). |
| `planner` | 1.1.0 | `make_chart` can pick the chart type itself. |
| `critic` | 1.1.0 | Verify the numbers flagged by the automated consistency check. |
| `structured_report_rules` | 1.0.0 | New; appended to the planner prompt when structured reports are on. |
| `memory_rules` | 1.0.0 | New; appended to the planner prompt when memory is on. |

## Eval note

Leave `AGENT_MEMORY_ENABLED` off for benchmark runs. Several tasks share each benchmark dataset,
so a stored finding from one task would be fed to the next, and the scores would no longer
measure the agent alone.
