"""Streamlit UI for the data analyst agent.

Talks to the FastAPI service only over HTTP, so it runs in its own container.

    AGENT_API_URL=http://localhost:8000 streamlit run frontend/app.py

Pick a bundled sample in the sidebar (API with ``AGENT_SAMPLES_ENABLED=true``) or upload a file,
ask a question, and watch the step timeline stream in. Gated steps (``AGENT_APPROVAL_ENABLED``)
pause the run on an approve / deny card.

On Cloud Run, set ``AGENT_API_ID_TOKEN=true`` to call a private API service with a Google ID
token from the metadata server (see deploy/cloudrun.md).
"""

from __future__ import annotations

import html
import json
import os
import threading
import time
from collections.abc import Generator, Iterator
from typing import Any

import httpx
import streamlit as st
from ui_helpers import (
    Step,
    iter_sse,
    model_costs,
    scalar_numbers,
    split_report,
    step_html,
    step_view,
)

API_URL = os.environ.get("AGENT_API_URL", "http://localhost:8000").rstrip("/")
USE_ID_TOKEN = os.environ.get("AGENT_API_ID_TOKEN", "").lower() in {"1", "true", "yes"}
METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"
)

st.set_page_config(page_title="Data Analyst Agent", page_icon="📊", layout="centered")

CSS = """
<style>
#MainMenu, footer, [data-testid="stToolbar"] {visibility: hidden;}
.block-container {padding-top: 2.5rem; max-width: 840px;}
h1 {font-weight: 650; letter-spacing: -0.02em;}
button[kind^="primary"] {background: #4f46e5; border-color: #4f46e5;}
button[kind^="primary"]:hover {background: #4338ca; border-color: #4338ca;}
.step {display: flex; gap: .7rem; align-items: flex-start; font-size: .92rem; line-height: 1.5;}
.step-tag {flex: 0 0 4.8rem; text-align: center; font-size: .66rem; font-weight: 600;
  text-transform: uppercase; letter-spacing: .05em; border-radius: 999px; padding: .15rem 0;
  margin-top: .12rem; background: rgba(100, 116, 139, .14); color: rgb(100, 116, 139);}
.tone-ok .step-tag {background: rgba(22, 163, 74, .14); color: rgb(22, 163, 74);}
.tone-warn .step-tag {background: rgba(217, 119, 6, .16); color: rgb(217, 119, 6);}
.tone-error .step-tag {background: rgba(220, 38, 38, .14); color: rgb(220, 38, 38);}
.step-body {flex: 1; min-width: 0; overflow-wrap: anywhere;}
.step-body code {font-size: .85em; padding: .05rem .3rem; border-radius: .25rem;
  background: rgba(100, 116, 139, .12);}
.step-detail {white-space: pre-wrap; opacity: .85; margin-top: .15rem;}
.step-list {margin: .2rem 0 0 1.1rem; padding: 0; opacity: .85;}
.step details summary {cursor: pointer; font-size: .8rem; opacity: .65;}
.step details pre {font-size: .78rem; max-height: 18rem; overflow: auto; padding: .5rem;
  border-radius: .4rem; background: rgba(100, 116, 139, .08); white-space: pre;}
.ds-meta {color: rgb(120, 120, 130); font-size: .85rem;}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


# ---- API client ----------------------------------------------------------------------------


class GoogleIdTokenAuth(httpx.Auth):
    """Bearer ID token for a private Cloud Run service, refreshed before its 1h expiry."""

    def __init__(self, audience: str) -> None:
        self._audience = audience
        self._token, self._fetched_at = "", 0.0
        self._lock = threading.Lock()

    def _get_token(self) -> str:
        with self._lock:
            if time.time() - self._fetched_at > 45 * 60:
                resp = httpx.get(
                    METADATA_TOKEN_URL,
                    params={"audience": self._audience},
                    headers={"Metadata-Flavor": "Google"},
                    timeout=5,
                )
                resp.raise_for_status()
                self._token, self._fetched_at = resp.text, time.time()
            return self._token

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._get_token()}"
        yield request


@st.cache_resource
def api() -> httpx.Client:
    auth = GoogleIdTokenAuth(API_URL) if USE_ID_TOKEN else None
    return httpx.Client(base_url=API_URL, timeout=httpx.Timeout(30.0, read=None), auth=auth)


@st.cache_data(ttl=300, show_spinner=False)
def load_samples() -> list[dict[str, Any]]:
    try:
        resp = api().get("/datasets/samples", timeout=5)
    except httpx.HTTPError:
        return []
    return resp.json() if resp.status_code == 200 else []  # older API: no samples route


@st.cache_data(show_spinner=False, max_entries=64)
def fetch_artifact(url: str) -> bytes | None:
    # Fetched server-side: the viewer's browser may not be able to reach the API.
    resp = api().get(url)
    return resp.content if resp.status_code == 200 else None


def detail_of(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("detail", resp.text))
    except ValueError:
        return resp.text


def stream_events(job_id: str, after: int = 0) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(event, data)`` from the job's SSE stream (events after seq ``after``)."""
    headers = {"Last-Event-ID": str(after)} if after else {}
    with api().stream("GET", f"/jobs/{job_id}/events", headers=headers) as resp:
        resp.raise_for_status()
        yield from iter_sse(resp.iter_lines())


# ---- state ---------------------------------------------------------------------------------

S = st.session_state
S.setdefault("dataset", None)  # {dataset_id, filename, columns, title, source, ...}
S.setdefault("events", [])  # [(event, data)] of the current job, as streamed
S.setdefault("job_id", None)
S.setdefault("following", False)
S.setdefault("pending", None)
S.setdefault("last_seq", 0)
S.setdefault("auto_ask", None)


def reset_job() -> None:
    S.update(job_id=None, events=[], following=False, pending=None, last_seq=0, final_status=None)


def use_dataset(info: dict[str, Any], source: str) -> None:
    S.dataset = {**info, "source": source}
    reset_job()


def start_job(question: str) -> None:
    resp = api().post("/ask", json={"dataset_id": S.dataset["dataset_id"], "question": question})
    if resp.status_code != 202:
        st.error(detail_of(resp))
        return
    reset_job()
    S.update(job_id=resp.json()["job_id"], following=True, asked=question)


def on_sample_change() -> None:
    sample = next((s for s in load_samples() if s["dataset_id"] == S.sample_pick), None)
    if sample:
        use_dataset(sample, "sample")


def on_example(question: str) -> None:
    S.question = question
    S.auto_ask = question


# ---- rendering -----------------------------------------------------------------------------


def render_step(step: Step) -> None:
    st.html(step_html(step))
    if step.code:
        st.code(step.code, language=step.lang or None, wrap_lines=True)


def render_event(event: str, data: dict[str, Any]) -> None:
    step = step_view(event, data)
    if step is not None:
        render_step(step)


def running_cost() -> float:
    return sum(float(d.get("usd") or 0) for e, d in S.events if e == "llm_call")


def timeline_label(status: str | None = None) -> str:
    steps = sum(1 for e, d in S.events if step_view(e, d) is not None)
    calls = sum(1 for e, _ in S.events if e == "llm_call")
    stats = f"{steps} steps · {calls} LLM calls · ${running_cost():.4f}"
    if status:
        return f"Done — {status} · {stats}"
    return f"Agent working… · {stats}"


def follow_job(job_id: str) -> None:
    """Stream the job's steps; stop early when it needs approval (the card takes over)."""
    with st.status(timeline_label(), expanded=True) as box:
        for event, data in S.events:
            render_event(event, data)
        for event, data in stream_events(job_id, after=S.last_seq):
            S.last_seq = data.get("seq", S.last_seq)
            if event != "end":
                S.events.append((event, data))
            if event == "llm_call":
                box.update(label=timeline_label())
            render_event(event, data)
            if event == "approval_required":
                S.pending = data
                box.update(label="Waiting for your approval…", state="running")
                st.rerun()
            if event == "end":
                S.following = False
                S.final_status = data.get("status")
                st.rerun()


def render_timeline() -> None:
    """The step log after a rerun (job paused or finished)."""
    waiting = bool(S.pending)
    status = S.get("final_status")
    label = "Waiting for your approval…" if waiting else timeline_label(status)
    state = "running" if waiting else ("error" if status == "failed" else "complete")
    with st.status(label, state=state, expanded=waiting):
        for event, data in S.events:
            render_event(event, data)


def render_approval_panel(job_id: str, action: dict[str, Any]) -> None:
    """Approve / deny a gated step (``POST /jobs/{id}/approve``)."""
    decision = None
    with st.container(border=True):
        st.markdown(f"#### ✋ Approve `{action.get('tool')}`?")
        for reason in action.get("reasons") or []:
            st.markdown(f"- {reason}")
        args = action.get("input") or {}
        src = args.get("query") or args.get("code")
        if src:
            st.code(src, language="sql" if action.get("tool") == "run_sql" else "python")
        if action.get("timeout_s"):
            st.caption(f"Denied automatically after {action['timeout_s']:g}s without a decision.")
        note = st.text_input("Note for the agent (optional)", key=f"note_{action.get('id')}")
        c1, c2 = st.columns(2)
        if c1.button("Approve", type="primary", width="stretch", key=f"ok_{action.get('id')}"):
            decision = True
        if c2.button("Deny", width="stretch", key=f"no_{action.get('id')}"):
            decision = False
    if decision is None:
        return
    resp = api().post(
        f"/jobs/{job_id}/approve",
        json={"approve": decision, "note": note or None, "action_id": action.get("id")},
    )
    if resp.status_code == 409:
        st.warning(detail_of(resp))
    elif resp.status_code != 200:
        st.error(detail_of(resp))
        return
    S.pending = None
    st.rerun()


def render_report(job: dict[str, Any], markdown: str) -> None:
    result = job.get("result") or {}
    numbers = scalar_numbers(result.get("key_numbers") or {})
    if numbers:
        st.markdown("**Key numbers**")
        for i in range(0, len(numbers), 3):
            cols = st.columns(3)
            for col, (label, value) in zip(cols, numbers[i : i + 3], strict=False):
                col.metric(label, value)

    shown: set[str] = set()
    for kind, value, alt in split_report(markdown):
        if kind == "md":
            st.markdown(value)
        elif value.startswith("/artifacts/") and (img := fetch_artifact(value)):
            st.image(img, caption=alt or None, width="stretch")
            shown.add(value)
        else:
            st.markdown(f"![{alt}]({value})")
    # Charts the report doesn't reference still belong with it.
    for art in result.get("artifacts") or []:
        if art["url"] not in shown and (img := fetch_artifact(art["url"])):
            st.image(img, caption=art["path"].rsplit("/", 1)[-1], width="stretch")


def render_costs(job: dict[str, Any], trace: list[tuple[str, dict[str, Any]]]) -> None:
    cost = job.get("cost") or {}
    duration = job.get("duration_seconds")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Cost", f"${cost.get('usd', 0):.4f}")
    c2.metric("Input tokens", f"{cost.get('input_tokens', 0):,}")
    c3.metric("Output tokens", f"{cost.get('output_tokens', 0):,}")
    c4.metric("Time", f"{duration:.1f}s" if duration else "—")
    rows = model_costs(trace)
    if not rows:
        st.caption("No LLM calls recorded.")
        return
    st.dataframe(
        rows,
        hide_index=True,
        width="stretch",
        column_config={
            "model": "Model",
            "purposes": "Used for",
            "calls": st.column_config.NumberColumn("Calls"),
            "input_tokens": st.column_config.NumberColumn("Input", format="localized"),
            "output_tokens": st.column_config.NumberColumn("Output", format="localized"),
            "cache_read_tokens": st.column_config.NumberColumn("Cache read", format="localized"),
            "cache_write_tokens": st.column_config.NumberColumn("Cache write", format="localized"),
            "usd": st.column_config.NumberColumn("USD", format="$%.4f"),
            "latency_ms": st.column_config.NumberColumn("Latency (ms)", format="localized"),
        },
    )
    if cost.get("cache_read_tokens"):
        st.caption(f"Prompt cache: {cost['cache_read_tokens']:,} input tokens read from cache.")


def render_trace(job_id: str, trace: list[dict[str, Any]]) -> None:
    types = sorted({e["type"] for e in trace})
    picked = st.multiselect("Event types", types, default=types, key=f"trace_types_{job_id}")
    shown = [e for e in trace if e["type"] in picked]
    st.caption(f"{len(shown)} of {len(trace)} events · expand an event to see its payload")
    st.json(shown, expanded=1)
    st.download_button(
        "Download trace (JSON)",
        json.dumps(trace, indent=2, default=str),
        file_name=f"{job_id}-trace.json",
        mime="application/json",
    )


def render_result(job_id: str) -> None:
    job = api().get(f"/jobs/{job_id}").json()
    status = job["status"]
    if status == "failed":
        st.error(f"Job failed: {job.get('error')}")
    elif status == "partial":
        st.warning("The agent stopped early (budget or turn limit); the report may be incomplete.")

    trace = job.get("trace") or []
    pairs = [(e["type"], e.get("data") or {}) for e in trace]
    resp = api().get(f"/jobs/{job_id}/report")
    report = resp.text if resp.status_code == 200 else ""

    tab_report, tab_cost, tab_trace = st.tabs(["Report", "Cost & tokens", "Raw trace"])
    with tab_report:
        if report:
            render_report(job, report)
        else:
            st.info("No report was produced.")
    with tab_cost:
        render_costs(job, pairs)
    with tab_trace:
        render_trace(job_id, trace)


# ---- sidebar -------------------------------------------------------------------------------

samples = load_samples()
with st.sidebar:
    st.markdown("### Try it")
    if samples:
        by_id = {s["dataset_id"]: s for s in samples}
        if S.dataset is None:
            use_dataset(samples[0], "sample")
        current = S.dataset["dataset_id"] if S.dataset["source"] == "sample" else None
        S.setdefault("sample_pick", current or samples[0]["dataset_id"])
        st.selectbox(
            "Sample dataset",
            list(by_id),
            format_func=lambda i: by_id[i]["title"],
            key="sample_pick",
            on_change=on_sample_change,
        )
        if S.dataset["source"] == "upload":
            st.button(
                "Use this sample instead",
                on_click=on_sample_change,
                width="stretch",
                type="tertiary",
            )
        active = by_id.get(S.dataset["dataset_id"])
        if active and active.get("questions"):
            st.markdown("**Example questions**")
            for i, q in enumerate(active["questions"]):
                st.button(q, key=f"ex_{i}", on_click=on_example, args=(q,), width="stretch")

    with st.expander("Upload your own data", expanded=not samples):
        uploaded = st.file_uploader(
            "CSV, Parquet, SQLite or DuckDB",
            type=["csv", "parquet", "sqlite", "sqlite3", "db", "duckdb"],
        )
    if uploaded is not None and S.get("upload_id") != uploaded.file_id:
        with st.spinner("Uploading…"):
            resp = api().post("/datasets", files={"file": (uploaded.name, uploaded.getvalue())})
        if resp.status_code == 201:
            S.upload_id = uploaded.file_id
            use_dataset({**resp.json(), "title": uploaded.name}, "upload")
        else:
            st.error(detail_of(resp))

    st.divider()
    try:
        health = api().get("/healthz", timeout=3).json()
        st.caption(f"🟢 API `{API_URL}` · store: {health['job_store']}")
    except (httpx.HTTPError, ValueError, KeyError):
        st.caption(f"🔴 API unreachable at `{API_URL}`")


# ---- page ----------------------------------------------------------------------------------

st.title("Data Analyst Agent")
st.caption(
    "Ask a question about a table. The agent plans, runs SQL and Python, checks its own work "
    "and writes a report."
)

dataset = S.dataset
if not dataset:
    st.info("Upload a data file in the sidebar to get started.")
    st.stop()

cols = dataset.get("columns") or []
meta = [f"{len(cols)} columns"]
if dataset.get("rows"):
    meta.insert(0, f"{dataset['rows']:,} rows")
source = "sample" if dataset["source"] == "sample" else "your upload"
with st.container(border=True):
    st.markdown(
        f"**{html.escape(dataset.get('title') or dataset['filename'])}** "
        f'<span class="ds-meta">· {html.escape(dataset["filename"])} · {source} · '
        f"{' · '.join(meta)}</span>",
        unsafe_allow_html=True,
    )
    if dataset.get("description"):
        st.caption(dataset["description"])
    st.markdown(
        " ".join(f"`{c}`" for c in cols[:16]) + (f" … +{len(cols) - 16}" if len(cols) > 16 else "")
    )

with st.form("ask", border=False):
    question = st.text_area(
        "Question",
        key="question",
        placeholder="Which region grew fastest last quarter?",
        height=80,
    )
    submitted = st.form_submit_button("Ask", type="primary", width="stretch")

to_ask = S.auto_ask or (question.strip() if submitted else "")
S.auto_ask = None
if to_ask:
    start_job(to_ask)

job_id = S.job_id
if job_id:
    if S.following and not S.pending:
        follow_job(job_id)
    elif S.events:
        render_timeline()

    if S.pending:
        render_approval_panel(job_id, S.pending)
    elif not S.following:
        render_result(job_id)
