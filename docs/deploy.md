# Docker images: build, size, sandbox requirements

One `Dockerfile`, two runnable targets. [deploy/cloudrun.md](../deploy/cloudrun.md) has the Cloud
Run commands; this page covers the images themselves and what the Python sandbox needs from the
container runtime.

| Target | Image (compose tag) | Contents |
|---|---|---|
| `api` | `analyst-agent-api:local` | FastAPI + agent + sandbox (`--extra api`), sample CSVs, non-root `app` user |
| `frontend` | `analyst-agent-frontend:local` | Streamlit + httpx only, pinned to `uv.lock` |

## Build

```bash
docker build --target api -t analyst-agent-api:local .
```

```bash
docker build --target frontend -t analyst-agent-frontend:local .
```

or both at once with `docker compose build` (`scripts/demo.sh` builds and starts them).

Build args:

| Arg | Default | Purpose |
|---|---|---|
| `INSTALL_EMBEDDINGS` | `false` | `true` adds the `embeddings` extra (sentence-transformers + torch) for `AGENT_SEMANTIC_BACKEND=embedding\|hybrid` |
| `TORCH_INDEX_URL` | `https://download.pytorch.org/whl/cpu` | where torch comes from when `INSTALL_EMBEDDINGS=true`; e.g. `.../whl/cu128` for a GPU image |
| `PYTHON_IMAGE` | `python:3.11-slim-bookworm` | base image for every stage (a registry mirror, say) |
| `UV_IMAGE` | `ghcr.io/astral-sh/uv:0.11.32` | source of the `uv` binary (build stages only) |

`docker-compose.yml` passes all four through from the shell or `.env`.

### Embeddings: CPU-only torch

`uv.lock` resolves torch from PyPI. On Linux that wheel depends on ~20 `nvidia-*` / `cuda-*`
packages and `triton`, several GB that a CPU-only Cloud Run instance never uses. With
`INSTALL_EMBEDDINGS=true` the build:

1. exports the locked `api` + `embeddings` requirements from `uv.lock`;
2. installs all of them except torch, `triton` and the `nvidia-*` / `cuda-*` packages exactly as
   locked (`--no-deps`);
3. installs the **locked torch version** (`torch==2.15.0` today) from `TORCH_INDEX_URL`, letting
   that build pull in only what it declares itself: for the CPU build, nothing beyond step 2;
4. runs `uv pip check`.

The lock itself is unchanged and stays universal, so `uv sync --extra embeddings` on a GPU dev
machine still gets CUDA torch. Why not a `[tool.uv.index]` + `[tool.uv.sources]` entry in
`pyproject.toml`: it would force CPU torch on every Linux checkout, and relocking needs
`download.pytorch.org`, which the environment this was written in cannot reach (see
*Verification status*).

The `embeddings-image` job in `.github/workflows/docker.yml` builds this variant and fails if
torch reports a CUDA version or any `nvidia-*`, `cuda-*` or `triton` distribution is installed.

## Image sizes

Measured with `docker image ls` (uncompressed, on disk; "content" = compressed layers, roughly
what a registry stores and Cloud Run pulls), linux/amd64:

| Image | On disk | Content (compressed) | Measured |
|---|---|---|---|
| `api` (default) | 983 MB | 221 MB | 2026-11-04, Docker 29.9.2 |
| `frontend` | 831 MB | 190 MB | 2026-11-04, Docker 29.9.2 |
| `api`, `INSTALL_EMBEDDINGS=true` (CPU torch) | not measured yet | not measured yet | needs `download.pytorch.org`; the `embeddings-image` CI job prints it in its summary |

Both measured images were built from `python:3.11-slim-bookworm` (198 MB of the above). See
*Verification status* for the two build-time substitutions used in that environment; neither
changes the installed packages. The CI `images` job writes the sizes from a clean runner to its
job summary; update this table from there when dependencies change.

## The sandbox inside a container

`run_python` relies on two kernel features, applied by `sandbox/_child/_oslock.py` to every
sandbox child just before user code ([sandbox.md](sandbox.md)):

| Layer | Needed from the runtime | If missing |
|---|---|---|
| **seccomp-bpf** (process, socket, rlimit, … denials; H1) | the `seccomp(2)` and `prctl(PR_SET_NO_NEW_PRIVS)` syscalls | **fail closed**: every `run_python` returns `SandboxSetupError: kernel confinement failed; refusing to run` |
| **Landlock** (filesystem and TCP; H2) | kernel ≥ 5.13 with Landlock enabled in the LSM list, and the `landlock_*` syscalls allowed by the runtime's seccomp profile | **best effort**: the child writes `[sandbox] Landlock unavailable (…)` to its stderr and file access relies on the audit hook alone |

### Docker

No special flags. Verified (below) with Docker 29.9.2's **default** seccomp profile, which
allows `seccomp(2)` and the `landlock_*` syscalls, and also with each of:

* `--security-opt no-new-privileges --cap-drop ALL` (recommended for production)
* `--read-only --tmpfs /tmp` (the data and artifacts paths are volumes)
* `--security-opt seccomp=unconfined`

The sandbox installs its own filter *on top of* the runtime's, so it does not need
`--privileged`, `CAP_SYS_ADMIN` or `seccomp=unconfined`. Do **not** use a custom seccomp profile
that denies `seccomp` (the sandbox then refuses to run, verified) or the `landlock_*` syscalls
(Landlock is then silently skipped, verified). An older Docker Engine whose default profile
predates Landlock behaves like the latter; check with the probe script below.

### Cloud Run

Not verified here (no GCP project in this environment). What the code needs, mapped to Cloud
Run's two execution environments:

* **Second generation** (`--execution-environment gen2`, a Linux microVM): a full Linux kernel
  and syscall surface, so seccomp installs. Whether Landlock is enabled depends on Google's
  kernel; check it with the job below. **Use gen2** for the API service:

  ```bash
  gcloud run services update analyst-agent-api --region $REGION --execution-environment gen2
  ```

* **First generation** (gVisor): a reimplemented syscall layer. seccomp filters are supported
  by gVisor; Landlock may not be, in which case the sandbox falls back to the audit hook as
  described above.

To check a given environment, run the image's own confinement code as a one-off Cloud Run job.
It prints the Landlock ABI (0 = unavailable) and any warning, and exits non-zero if seccomp
cannot be installed (`^@^` makes `@` the list separator, since the code contains commas):

```bash
gcloud run jobs create sandbox-check --region $REGION --image $IMAGE_BASE/api:$TAG \
  --execution-environment gen2 --command python --args="^@^-c@import sys, analyst_agent.sandbox as s; sys.path.insert(0, s.__path__[0] + '/_child'); import _oslock; print('landlock ABI', _oslock.landlock_abi()); print('warnings', _oslock.confine([sys.prefix, sys.base_prefix], ['/tmp']))"
```

```bash
gcloud run jobs execute sandbox-check --region $REGION --wait
```

```bash
gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="sandbox-check"' --limit 5 --format='value(textPayload)'
```

### Fallback when the host has no Landlock

If the check prints `landlock ABI 0` or a `Landlock unavailable` warning, the sandbox still runs
and seccomp still blocks process creation (H1), sockets and the other syscalls. File access is
then guarded only by the Python audit hook, which native code can bypass; in the API image that
risk is smaller because pyarrow (the H2 vector) is not installed there, but other native readers
may exist. Compensate at the container level:

* run as the non-root `app` user (the image default) with `--read-only` and `--cap-drop ALL`;
* keep secrets out of files the service user can read; prefer Secret Manager env vars, which the
  sandbox's scrubbed environment never passes on;
* give the API container no egress except the Anthropic and Langfuse endpoints (a VPC
  connector with firewall rules on Cloud Run, `--network` rules locally);
* do not mount anything into the container that a sandbox must not read (other tenants' data,
  credentials, the Docker socket).

## Verifying an image

Sandbox probes (docs/review.md H1/H2) inside the API image, through the API and against the
kernel layers alone; extra arguments go to `docker run`:

```bash
scripts/sandbox-probes/run.sh analyst-agent-api:local --security-opt no-new-privileges --cap-drop ALL
```

On a host without Landlock, `EXPECT_LANDLOCK=false scripts/sandbox-probes/run.sh` checks the
fallback instead. The UI end to end against the compose stack, with every `AGENT_*_ENABLED` flag
on (needs Node and Playwright, see [scripts/ui-e2e/README.md](../scripts/ui-e2e/README.md)):

```bash
scripts/ui-e2e/run.sh --docker /tmp/ui-e2e-docker
```

CI runs both, plus the embeddings build, on every PR that touches `Dockerfile`,
`.dockerignore`, `docker-compose.yml`, `pyproject.toml` or `uv.lock`
(`.github/workflows/docker.yml`).

## Verification status (2026-11-04)

Run in a cloud sandbox with a Docker 29.9.2 daemon, kernel 6.18 (Landlock ABI 7), cgroup v1:

| Check | Result |
|---|---|
| `api` and `frontend` images build | yes |
| compose stack, every `AGENT_*_ENABLED` flag on, samples on | healthy; a real `core_runner` job runs routing (falls back to L3 without a key), semantic retrieval, memory retrieval, saves its trace, and fails at the first LLM call as expected with no API key |
| `scripts/ui-e2e/run.sh --docker` | 24/24 checks |
| `scripts/sandbox-probes/run.sh` | all pass under the default profile, `no-new-privileges` + `cap-drop ALL`, `--read-only`, `seccomp=unconfined`; fallback verified with a profile hiding `landlock_*`; fail-closed verified with a profile denying `seccomp` |
| `INSTALL_EMBEDDINGS=true` build | **not run**: `download.pytorch.org` is blocked there. The step's mechanics were exercised with `TORCH_INDEX_URL=https://pypi.org/simple` |
| Cloud Run | not run |

That environment's egress policy also blocks `ghcr.io` blob downloads and re-signs TLS with
its own CA, so two build args were overridden, with no change to the `Dockerfile`:
`UV_IMAGE` pointed at a local image holding the same `uv` 0.11.32 binary (from the PyPI wheel),
and `PYTHON_IMAGE` at `python:3.11-slim-bookworm` (pulled from Docker Hub) plus the proxy's CA
bundle and `SSL_CERT_FILE`. A plain `docker compose up --build` on an unrestricted network is
what the `images` CI job runs.
