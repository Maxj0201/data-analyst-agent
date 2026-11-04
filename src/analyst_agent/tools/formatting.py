"""Formatting helpers shared by tool handlers."""

from __future__ import annotations

from typing import Any


def rows_to_markdown(columns: list[str], rows: list[dict[str, Any]], limit: int = 20) -> str:
    if not columns:
        return "(no columns)"

    def cell(v: Any) -> str:
        return "" if v is None else str(v).replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for r in rows[:limit]:
        lines.append("| " + " | ".join(cell(r.get(c)) for c in columns) + " |")
    if len(rows) > limit:
        lines.append(f"... ({len(rows) - limit} more rows not shown)")
    return "\n".join(lines)
