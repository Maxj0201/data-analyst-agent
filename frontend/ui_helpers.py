"""Pure helpers for the Streamlit UI (no Streamlit import, so they are unit-tested directly).

``step_view`` turns one SSE event into a timeline row, ``model_costs`` aggregates ``llm_call``
events per model, and ``split_report`` cuts the report Markdown at its chart images so the UI can
render each chart inline where the report references it.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

IMAGE_MD = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")


@dataclass
class Step:
    """One row of the step timeline."""

    kind: str
    """Tag shown on the left (``plan``, ``route``, ``tool``, ``result``, ``critic``, ...)."""
    title: str
    """One line; only `code` and **bold** are formatted (see ``inline_html``)."""
    tone: str = "info"
    """``info`` | ``ok`` | ``warn`` | ``error`` (colours the tag)."""
    detail: str = ""
    """Plain text under the title (line breaks kept)."""
    more: str = ""
    """Plain text behind a collapsed "details" toggle (e.g. a full tool output)."""
    code: str = ""
    lang: str = ""
    bullets: list[str] = field(default_factory=list)


def inline_html(text: str) -> str:
    """Escape ``text`` and render only `code` and **bold**: agent output is never raw HTML."""
    out = html.escape(text, quote=False)
    out = _CODE.sub(r"<code>\1</code>", out)
    return _BOLD.sub(r"<strong>\1</strong>", out)


def step_html(step: Step) -> str:
    """The timeline row as one HTML block (the caller renders ``step.code`` separately)."""
    parts = [
        f'<div class="step tone-{html.escape(step.tone)}">',
        f'<span class="step-tag">{html.escape(step.kind)}</span>',
        f'<div class="step-body"><div>{inline_html(step.title)}</div>',
    ]
    if step.detail:
        parts.append(f'<div class="step-detail">{inline_html(step.detail)}</div>')
    if step.bullets:
        items = "".join(f"<li>{inline_html(b)}</li>" for b in step.bullets)
        parts.append(f'<ul class="step-list">{items}</ul>')
    if step.more:
        parts.append(
            f"<details><summary>details</summary><pre>{html.escape(step.more)}</pre></details>"
        )
    parts.append("</div></div>")
    return "".join(parts)


def summarize_output(summary: str, limit: int = 160) -> tuple[str, str]:
    """``(one line, full text or "")`` for a tool result summary that may be Markdown."""
    text = summary.strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "", ""
    first = lines[0].lstrip("#").strip()
    if first.startswith("|"):
        rows = sum(1 for ln in lines if ln.startswith("|")) - 2  # header + separator
        first = f"{max(rows, 0)} row{'s' if rows != 1 else ''}"
    one = _clip(first, limit)
    return one, (text if len(lines) > 1 or one != text else "")


def _clip(text: Any, limit: int) -> str:
    s = str(text)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def step_view(event: str, d: dict[str, Any]) -> Step | None:
    """Timeline row for one agent event (``None`` = not shown, e.g. ``llm_call``)."""
    if event == "plan":
        return Step("plan", "**Plan**", detail=_clip(d.get("plan", ""), 1500))
    if event == "route":
        fallback = " · router fallback" if d.get("source") == "fallback" else ""
        reason = f" — {d['reason']}" if d.get("reason") else ""
        title = f"**Routed to {d.get('tier')}** · `{d.get('model')}`{fallback}{reason}"
        return Step("route", title)
    if event == "escalation":
        return Step(
            "route",
            f"**Escalated {d.get('from_tier')} → {d.get('to_tier')}** · `{d.get('to_model')}` "
            f"({d.get('trigger')})",
            tone="warn",
        )
    if event == "semantic_retrieval":
        metrics = d.get("metrics") or []
        names = ", ".join(f"`{m}`" for m in metrics) or "none matched"
        tone = "warn" if d.get("error") else "info"
        detail = f"Retrieval error: {d['error']}" if d.get("error") else ""
        return Step("metrics", f"**Metric definitions** — {names}", tone=tone, detail=detail)
    if event == "tool_call":
        args = d.get("input") or {}
        name = str(d.get("name"))
        if "query" in args:
            code, lang = str(args["query"]), "sql"
        elif "code" in args:
            code, lang = str(args["code"]), "python"
        elif name == "final_report":
            code, lang = "", ""
        else:  # other tools: JSON arguments stay behind a toggle
            return Step(
                "tool", f"**{name}**", more=_clip(json.dumps(args, indent=2, default=str), 3000)
            )
        return Step("tool", f"**{name}**", code=_clip(code, 3000), lang=lang)
    if event == "tool_result":
        ok = d.get("ok", True)
        one, full = summarize_output(str(d.get("summary") or ""))
        return Step(
            "result",
            f"{d.get('name')} → {one}",
            tone="ok" if ok else "error",
            more=_clip(full, 6000),
        )
    if event == "critic":
        verdict = str(d.get("verdict", "?"))
        issues = [str(i) for i in d.get("issues") or []]
        fix = f"Suggested fix: {d['suggested_fix']}" if d.get("suggested_fix") else ""
        return Step(
            "critic",
            f"**Critic: {verdict}**",
            tone="ok" if verdict == "pass" else "warn",
            bullets=issues,
            detail=fix,
        )
    if event == "approval_required":
        return Step(
            "approval",
            f"**Approval required** · `{d.get('tool')}`",
            tone="warn",
            bullets=[str(r) for r in d.get("reasons") or []],
        )
    if event == "approval_decision":
        approved = bool(d.get("approved"))
        verdict = "approved" if approved else "denied"
        note = f" — {d['note']}" if d.get("note") else ""
        return Step(
            "approval",
            f"{d.get('tool')} {verdict} ({d.get('decided_by')}){note}",
            tone="ok" if approved else "error",
        )
    if event == "stop":
        done = str(d.get("status")).startswith("completed")
        title = (
            f"**{'Finished' if done else 'Stopped'} ({d.get('status')})** — {d.get('detail', '')}"
        )
        return Step("stop", title, tone="ok" if done else "warn")
    if event == "error":
        return Step("error", str(d.get("message", "")), tone="error")
    return None


def iter_sse(lines: Iterable[str]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Parse ``(event, data)`` pairs from SSE lines; stops after the ``end`` event."""
    name: str | None = None
    data: list[str] = []
    for line in lines:
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())
        elif line == "":
            if name and data:
                yield name, json.loads("\n".join(data))
                if name == "end":
                    return
            name, data = None, []


def model_costs(events: Iterable[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """Per-model totals from ``llm_call`` events, most expensive first."""
    rows: dict[str, dict[str, Any]] = {}
    for event, d in events:
        if event != "llm_call":
            continue
        model = str(d.get("model") or "unknown")
        row = rows.setdefault(
            model,
            {
                "model": model,
                "purposes": set(),
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "usd": 0.0,
                "latency_ms": 0.0,
            },
        )
        row["calls"] += 1
        if d.get("purpose"):
            row["purposes"].add(str(d["purpose"]))
        for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            row[key] += int(d.get(key) or 0)
        row["usd"] += float(d.get("usd") or 0)
        row["latency_ms"] += float(d.get("latency_ms") or 0)
    out = []
    for row in rows.values():
        row["purposes"] = ", ".join(sorted(row["purposes"]))
        row["usd"] = round(row["usd"], 6)
        row["latency_ms"] = round(row["latency_ms"], 1)
        out.append(row)
    return sorted(out, key=lambda r: (-r["usd"], -r["input_tokens"], r["model"]))


def split_report(markdown: str) -> list[tuple[str, str, str]]:
    """``[("md", text, ""), ("img", url, alt), ...]`` in document order."""
    parts: list[tuple[str, str, str]] = []
    pos = 0
    for m in IMAGE_MD.finditer(markdown):
        if m.start() > pos:
            parts.append(("md", markdown[pos : m.start()], ""))
        parts.append(("img", m.group(2), m.group(1)))
        pos = m.end()
    if pos < len(markdown):
        parts.append(("md", markdown[pos:], ""))
    return [p for p in parts if p[0] == "img" or p[1].strip()]


def scalar_numbers(key_numbers: dict[str, Any], limit: int = 8) -> list[tuple[str, str]]:
    """Key numbers that fit a metric tile: ``(label, formatted value)``."""
    out = []
    for key, value in key_numbers.items():
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            continue
        if isinstance(value, float):
            text = f"{value:,.2f}" if abs(value) < 1e6 else f"{value:,.0f}"
        elif isinstance(value, int):
            text = f"{value:,}"
        else:
            text = _clip(value, 40)
        out.append((key.replace("_", " "), text))
        if len(out) >= limit:
            break
    return out
