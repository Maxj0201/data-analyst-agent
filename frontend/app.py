"""Streamlit UI for the data analyst agent.

Talks to the FastAPI service only over HTTP, so it runs in its own container.

    AGENT_API_URL=http://localhost:8000 streamlit run frontend/app.py

On Cloud Run, set ``AGENT_API_ID_TOKEN=true`` to call a private API service with a Google ID
token from the metadata server (see deploy/cloudrun.md).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Generator, Iterator

import httpx
import streamlit as st

API_URL = os.environ.get("AGENT_API_URL", "http://localhost:8000").rstrip("/")
USE_ID_TOKEN = os.environ.get("AGENT_API_ID_TOKEN", "").lower() in {"1", "true", "yes"}
METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"
)
IMAGE_MD = re.compile(r"!\[[^\]]*\]\([^)]*\)")

st.set_page_config(page_title="Data Analyst Agent", page_icon="📊", layout="centered")


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


def stream_events(job_id: str) -> Iterator[tuple[str, dict]]:
    """Yield ``(event, data)`` pairs from the job's SSE stream until it ends."""
    with api().stream("GET", f"/jobs/{job_id}/events") as resp:
        resp.raise_for_status()
        name, data = None, []
        for line in resp.iter_lines():
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


def describe(event: str, d: dict) -> str | None:
    """One human-readable line per agent step (``None`` = don't show)."""
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
    return None


def render_cost(job: dict) -> None:
    cost = job.get("cost") or {}
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Cost", f"${cost.get('usd', 0):.4f}")
    c2.metric("Input tokens", f"{cost.get('input_tokens', 0):,}")
    c3.metric("Output tokens", f"{cost.get('output_tokens', 0):,}")
    duration = job.get("duration_seconds")
    c4.metric("Time", f"{duration:.1f}s" if duration else "—")


def render_result(job_id: str) -> None:
    job = api().get(f"/jobs/{job_id}").json()
    status = job["status"]
    if status == "failed":
        st.error(f"Job failed: {job.get('error')}")
    elif status == "partial":
        st.warning("The agent stopped early (budget or turn limit); the report may be incomplete.")

    render_cost(job)

    resp = api().get(f"/jobs/{job_id}/report")
    if resp.status_code == 200:
        st.divider()
        # Charts are fetched server-side below: the browser may not be able to reach the API.
        st.markdown(IMAGE_MD.sub("", resp.text))

    artifacts = (job.get("result") or {}).get("artifacts") or []
    if artifacts:
        st.subheader("Charts")
        for art in artifacts:
            img = api().get(art["url"])
            if img.status_code == 200:
                st.image(img.content, caption=art["path"], width="stretch")

    key_numbers = (job.get("result") or {}).get("key_numbers")
    if key_numbers:
        with st.expander("Key numbers"):
            st.json(key_numbers)


# ---- page ----------------------------------------------------------------------------------

st.title("📊 Data Analyst Agent")
st.caption("Upload a data file, ask a question, and watch the agent work.")

uploaded = st.file_uploader(
    "Dataset (CSV, Parquet, SQLite, DuckDB)",
    type=["csv", "parquet", "sqlite", "sqlite3", "db", "duckdb"],
)
if uploaded is not None and st.session_state.get("upload_id") != uploaded.file_id:
    with st.spinner("Uploading…"):
        resp = api().post("/datasets", files={"file": (uploaded.name, uploaded.getvalue())})
    if resp.status_code == 201:
        info = resp.json()
        st.session_state.update(dataset=info, upload_id=uploaded.file_id, job_id=None, steps=[])
    else:
        st.error(resp.json().get("detail", resp.text))

dataset = st.session_state.get("dataset")
if dataset:
    st.success(
        f"**{dataset['filename']}** · {len(dataset['columns'])} columns: "
        + ", ".join(f"`{c}`" for c in dataset["columns"][:12])
        + (" …" if len(dataset["columns"]) > 12 else "")
    )

    with st.form("ask"):
        question = st.text_area("Question", placeholder="Which region grew fastest last quarter?")
        submitted = st.form_submit_button("Ask", type="primary", width="stretch")

    if submitted and question.strip():
        resp = api().post(
            "/ask", json={"dataset_id": dataset["dataset_id"], "question": question.strip()}
        )
        if resp.status_code != 202:
            st.error(resp.json().get("detail", resp.text))
        else:
            job_id = resp.json()["job_id"]
            st.session_state.update(job_id=job_id, steps=[])
            with st.status("Agent working…", expanded=True) as box:
                running_cost = 0.0
                for event, data in stream_events(job_id):
                    if event == "llm_call":
                        running_cost += float(data.get("usd") or 0)
                        box.update(label=f"Agent working… (${running_cost:.4f} so far)")
                    line = describe(event, data)
                    if line:
                        st.session_state.steps.append(line)
                        st.markdown(line)
                    if event == "end":
                        ok = data.get("status") != "failed"
                        box.update(
                            label=f"Done — {data.get('status')}",
                            state="complete" if ok else "error",
                            expanded=False,
                        )
    elif st.session_state.get("steps"):
        # Re-render the finished step log after Streamlit reruns.
        with st.status("Agent steps", state="complete", expanded=False):
            for line in st.session_state.steps:
                st.markdown(line)

    if st.session_state.get("job_id"):
        render_result(st.session_state.job_id)
else:
    st.info("Upload a data file to get started.")

with st.sidebar:
    st.markdown(f"**API:** `{API_URL}`")
    try:
        health = api().get("/healthz", timeout=3).json()
        st.markdown(f"Status: 🟢 {health['status']} · store: {health['job_store']}")
    except httpx.HTTPError:
        st.markdown("Status: 🔴 API unreachable")
