# syntax=docker/dockerfile:1.7
#
# Two runnable targets share one build:
#   docker build --target api      -t analyst-agent-api .
#   docker build --target frontend -t analyst-agent-frontend .
# The default (last) target is `api`.
#
# Opt-in build arg for the API image:
#   --build-arg INSTALL_EMBEDDINGS=true   adds the `embeddings` extra (sentence-transformers,
#                                         torch) so AGENT_SEMANTIC_BACKEND=embedding|hybrid
#                                         works. Off by default: it adds several GB.

ARG PYTHON_IMAGE=python:3.11-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.32

FROM ${UV_IMAGE} AS uv

# ---- build stages: resolve the locked environment into /app/.venv --------------------------
FROM ${PYTHON_IMAGE} AS builder-base
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app

FROM builder-base AS builder-api
ARG INSTALL_EMBEDDINGS=false
# Dependencies first (cached unless pyproject.toml / uv.lock change), then the project itself.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project --extra api \
        $( [ "$INSTALL_EMBEDDINGS" = "true" ] && echo --extra embeddings )
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# Built into the wheel as analyst_agent/semantic/metrics (pyproject force-include).
COPY benchmarks/metrics ./benchmarks/metrics
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --extra api \
        $( [ "$INSTALL_EMBEDDINGS" = "true" ] && echo --extra embeddings )

FROM builder-base AS builder-frontend
# The UI only talks HTTP to the API: install just streamlit + httpx (and their deps), pinned to
# the versions in uv.lock, without the agent's own dependencies (duckdb, anthropic, ...).
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv export --frozen --no-dev --extra frontend --no-emit-project --no-hashes \
        -o /tmp/constraints.txt \
 && uv venv /app/.venv \
 && uv pip install --python /app/.venv/bin/python -c /tmp/constraints.txt streamlit httpx

# ---- runtime base: no uv, no build tooling, non-root user ----------------------------------
FROM ${PYTHON_IMAGE} AS runtime
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app
WORKDIR /app
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MPLCONFIGDIR=/tmp/matplotlib

FROM runtime AS frontend
COPY --from=builder-frontend --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app frontend ./frontend
USER app
ENV PORT=8501 \
    AGENT_API_URL=http://api:8000 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/_stcore/health', timeout=4)"
CMD ["sh", "-c", "exec streamlit run frontend/app.py --server.address=0.0.0.0 --server.port=${PORT} --server.headless=true"]

FROM runtime AS api
COPY --from=builder-api --chown=app:app /app/.venv /app/.venv
# Semantic-layer metric definitions ship inside the installed wheel
# (analyst_agent/semantic/metrics), so AGENT_SEMANTIC_METRICS_DIR stays unset. The sample CSVs
# served by GET /datasets/samples (read-only, <1 MB) are copied here; the benchmark tasks and
# generator stay out of the image (.dockerignore).
COPY --chown=app:app benchmarks/data ./benchmarks/data
RUN mkdir -p /app/data/uploads /app/artifacts && chown -R app:app /app/data /app/artifacts
USER app
# Cloud Run injects PORT=8080; locally and in compose it defaults to 8000.
# Feature flags (AGENT_ROUTING_ENABLED, AGENT_SEMANTIC_LAYER_ENABLED, AGENT_APPROVAL_ENABLED)
# stay off unless set at run time; see docs/configuration.md.
# HF_HOME: where the embeddings backend (INSTALL_EMBEDDINGS=true) caches its model; on the
# data volume so it survives restarts.
ENV PORT=8000 \
    HF_HOME=/app/data/huggingface \
    AGENT_SAMPLES_DIR=/app/benchmarks/data
EXPOSE 8000
VOLUME ["/app/data", "/app/artifacts"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=4)"
CMD ["sh", "-c", "exec uvicorn analyst_agent.api.app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*' --timeout-graceful-shutdown 30"]
