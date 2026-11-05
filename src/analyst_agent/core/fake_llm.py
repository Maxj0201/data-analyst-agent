"""Scripted, offline stand-in for :class:`analyst_agent.core.llm.LLMClient`.

``FakeLLM`` replays pre-scripted responses per role (``agent`` / ``critic`` / ``report_writer``)
and never touches the network. The test suite uses it directly; ``analyst-agent ask --dry-run``
uses :func:`dry_run_llm` to script a plausible run over the loaded data so the whole pipeline
(loop, tools, sandbox, critic, report) can be exercised without an API key.
"""

from __future__ import annotations

import copy
import itertools
import json
import re
from collections.abc import Callable
from typing import Any

from analyst_agent.core.llm import CostTracker, LLMResponse, Usage
from analyst_agent.sandbox import quote_ident

_ids = itertools.count(1)

DRY_RUN_MODEL = "dry-run"


def text(t: str) -> dict[str, Any]:
    return {"type": "text", "text": t}


def tool_use(name: str, **inp: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"toolu_{next(_ids)}", "name": name, "input": inp}


def resp(
    *blocks: dict[str, Any],
    stop: str | None = None,
    usage: Usage | None = None,
    model: str = "claude-opus-5-5",
) -> LLMResponse:
    if stop is None:
        stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
    return LLMResponse(
        content=list(blocks),
        stop_reason=stop,
        usage=usage or Usage(input_tokens=100, output_tokens=50),
        model=model,
    )


def critic_json(
    verdict: str, issues: list[str] | None = None, fix: str = "", **resp_kw: Any
) -> LLMResponse:
    payload = {"verdict": verdict, "issues": issues or [], "suggested_fix": fix}
    return resp(text(json.dumps(payload)), **resp_kw)


class FakeLLM:
    """Scripted LLM: separate queues for the agent, the critic, the report writer and the router.

    ``calls`` records the requested ``model`` per call (``None`` = the configured default); the
    cost tracker is billed under the scripted response's ``model``.
    """

    def __init__(
        self,
        agent: list[LLMResponse | Exception],
        critic: list[LLMResponse | Exception] | None = None,
        writer: list[LLMResponse] | None = None,
        router: list[LLMResponse | Exception] | None = None,
    ) -> None:
        self.queues: dict[str, list[LLMResponse | Exception]] = {
            "agent": list(agent),
            "critic": list(critic or []),
            "report_writer": list(writer or []),
            "router": list(router or []),
        }
        self.calls: list[dict[str, Any]] = []

    def create(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        output_format: dict[str, Any] | None = None,
        model: str | None = None,
        role: str = "agent",
        cost: CostTracker | None = None,
        effort: str | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        self.calls.append(
            {
                "role": role,
                "system": system,
                "messages": copy.deepcopy(messages),
                "tools": tools,
                "output_format": output_format,
                "model": model,
                "effort": effort,
                "max_tokens": max_tokens,
            }
        )
        q = self.queues[role]
        if not q:
            raise AssertionError(f"FakeLLM: no scripted response left for role={role!r}")
        item = q.pop(0)
        if isinstance(item, Exception):
            raise item
        if cost is not None:
            cost.add(item.model, item.usage, role=role)
        return item

    def calls_for(self, role: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["role"] == role]


# --------------------------------------------------------------------------- dry run

SqlFn = Callable[[str], Any]  # query -> SqlResult

_NUMERIC = re.compile(r"^(TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT|DOUBLE|FLOAT|REAL|DECIMAL)")
_TEXT = re.compile(r"^(VARCHAR|TEXT|STRING)")
# Columns that plausibly mean "revenue"/"value" when the question's word is not a column name.
_MEASURE_HINTS = ("revenue", "sales", "amount", "mrr", "arr", "total", "price", "pay", "salary")


def _fmt(v: Any) -> str:
    return f"{v:,.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else f"{v:,}"


def _words(question: str) -> set[str]:
    ws = set(re.findall(r"[a-z0-9]+", question.lower()))
    return ws | {w[:-1] for w in ws if w.endswith("s")}


def _mentioned(col: str, words: set[str]) -> bool:
    return col.lower() in words or all(p in words for p in col.lower().split("_") if p)


def _pick_columns(cols: list[tuple[str, str]], question: str) -> tuple[str | None, str | None]:
    words = _words(question)
    text_cols = [c for c, t in cols if _TEXT.match(t.upper())]
    num_cols = [
        c for c, t in cols if _NUMERIC.match(t.upper()) and not re.search(r"(^|_)id$", c.lower())
    ]
    group = next((c for c in text_cols if _mentioned(c, words)), None)
    group = group or next((c for c in text_cols if not c.lower().endswith("id")), None)
    measure = next((c for c in num_cols if _mentioned(c, words)), None)
    measure = measure or next((c for h in _MEASURE_HINTS for c in num_cols if h in c.lower()), None)
    return group, measure or (num_cols[0] if num_cols else None)


def dry_run_llm(
    question: str, table: str, run_sql: SqlFn, *, structured_report: bool = False
) -> FakeLLM:
    """Script a deterministic run over ``table``: inspect schema, aggregate with SQL, chart it,
    report, critic passes (plus a router verdict, used only when routing is on).

    The query is chosen from the schema with a simple heuristic (a text column the question
    mentions to group by, a numeric column it mentions or that looks like revenue to sum) and
    pre-run once via ``run_sql`` so the scripted report quotes the real numbers. The agent loop
    then executes the same tool calls for real through the tool registry and sandbox. This
    exercises the plumbing; it is not an analysis.

    ``structured_report`` (``AGENT_STRUCTURED_REPORT_ENABLED``) submits ``final_report`` as the
    structured fields (headline, key_numbers, method, caveats, charts) instead of Markdown.
    """
    zero = Usage()  # no tokens are spent, so the cost summary reads $0
    kw: dict[str, Any] = {"usage": zero, "model": DRY_RUN_MODEL}

    lit = table.replace("'", "''")
    schema = run_sql(
        "SELECT column_name, data_type FROM information_schema.columns "
        f"WHERE table_name = '{lit}' ORDER BY ordinal_position"
    )
    cols = [(r["column_name"], r["data_type"]) for r in schema.rows] if schema.ok else []
    group, measure = _pick_columns(cols, question)

    agg = f"SUM({quote_ident(measure)})" if measure else "COUNT(*)"
    agg_name = f"total_{measure}" if measure else "row_count"
    if group:
        g = quote_ident(group)
        sql = (
            f"SELECT {g} AS {g}, {agg} AS {quote_ident(agg_name)} FROM {quote_ident(table)} "
            "GROUP BY 1 ORDER BY 2 DESC NULLS LAST LIMIT 10"
        )
    else:
        sql = f"SELECT {agg} AS {quote_ident(agg_name)} FROM {quote_ident(table)}"
    res = run_sql(sql)
    rows = res.rows if res.ok else []

    plan = (
        f"<plan>1. Inspect `{table}`. 2. "
        + (f"Aggregate {agg} by `{group}`. " if group else f"Compute {agg}. ")
        + "3. Chart it and report.</plan>"
    )
    agent: list[LLMResponse | Exception] = [
        resp(text(plan), tool_use("inspect_schema", table=table), **kw),
        resp(tool_use("run_sql", query=sql), **kw),
    ]

    key_numbers: dict[str, Any] = {}
    if rows and group:
        top = rows[0]
        key_numbers = {f"top_{group}": top[group], agg_name: top[agg_name]}
        agent.append(
            resp(
                tool_use(
                    "make_chart",
                    spec={
                        "chart_type": "bar",
                        "title": f"{agg_name} by {group}",
                        "x": [str(r[group]) for r in rows],
                        "y": [r[agg_name] for r in rows],
                    },
                ),
                **kw,
            )
        )
        headline = f"**{top[group]}** has the highest {agg_name} ({_fmt(top[agg_name])})."
    elif rows:
        key_numbers = {agg_name: rows[0][agg_name]}
        headline = f"{agg_name} is **{_fmt(rows[0][agg_name])}**."
    else:
        headline = "The query returned no rows" + (f": {res.error}" if res.error else ".")

    table_md = res.markdown_preview if res.ok else ""
    markdown = (
        f"# {question}\n\n> Dry run: scripted offline LLM, no API calls.\n\n{headline}\n\n"
        f"{table_md}\n\nQuery:\n\n```sql\n{sql}\n```\n"
    )
    if structured_report:
        report = tool_use(
            "final_report",
            headline=headline,
            key_numbers=key_numbers,
            method=f"Dry run: scripted offline LLM, no API calls. Query on `{table}`: {sql}",
            caveats=["Dry run: the query is chosen by a heuristic, not by analysis."],
            charts=[],  # make_chart's artifacts are still listed with the report
            details=table_md,
        )
    else:
        report = tool_use("final_report", markdown=markdown, key_numbers=key_numbers)
    agent.append(resp(report, **kw))
    # Only consumed when AGENT_ROUTING_ENABLED is on: a grouped query is L2, a single aggregate L1.
    tier = "L2" if group else "L1"
    route = json.dumps({"tier": tier, "reason": "dry run: scripted from the planned query shape"})
    return FakeLLM(
        agent=agent, critic=[critic_json("pass", **kw)], router=[resp(text(route), **kw)]
    )
