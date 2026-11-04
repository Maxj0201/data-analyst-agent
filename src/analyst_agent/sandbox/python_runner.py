"""Run agent-written Python in a separate, resource-limited interpreter.

Layers (see docs/sandbox.md for the threat model):

* **Process isolation** - a fresh ``python`` subprocess per call, in its own session /
  process group, killed as a group on timeout.
* **OS limits** (``resource.setrlimit`` in the child before ``exec``): address space
  (memory), CPU seconds, max file size, open files, no core dumps.
* **Scrubbed environment** - only a handful of variables; no API keys, proxies or HOME.
* **Temp cwd** - a fresh ``TemporaryDirectory`` that is deleted afterwards.
* **In-interpreter guard** (``_child/sitecustomize.py``) - blocks sockets at import time and
  installs an audit hook denying network, subprocesses, writes outside the temp dir, reads
  outside an allowlist, and imports outside pandas/numpy/matplotlib/scipy + safe stdlib.
"""

from __future__ import annotations

import json
import os
import resource
import signal
import site
import subprocess
import sys
import sysconfig
import tempfile
import time
import uuid
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd

from . import PyResult
from . import _errors as T
from ._loaders import DataSources, load_dataframes, normalize_sources, safe_identifier

DEFAULT_TIMEOUT_S = 60.0
DEFAULT_MEMORY_LIMIT_MB = 1024
MAX_OUTPUT_BYTES = 64 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_ARTIFACTS = 20
# Only inert file types leave the sandbox (no HTML/SVG: they can carry script if served).
ARTIFACT_SUFFIXES = {".png", ".jpg", ".jpeg", ".csv", ".json", ".txt", ".md"}

CHILD_DIR = Path(__file__).resolve().parent / "_child"

ALLOWED_THIRD_PARTY = ("pandas", "numpy", "matplotlib", "scipy")
ALLOWED_STDLIB = (
    "math",
    "cmath",
    "statistics",
    "decimal",
    "fractions",
    "numbers",
    "random",
    "datetime",
    "time",
    "calendar",
    "zoneinfo",
    "collections",
    "itertools",
    "functools",
    "operator",
    "heapq",
    "bisect",
    "array",
    "re",
    "string",
    "textwrap",
    "unicodedata",
    "difflib",
    "json",
    "csv",
    "io",
    "pprint",
    "reprlib",
    "typing",
    "dataclasses",
    "enum",
    "abc",
    "copy",
    "types",
    "contextlib",
    "warnings",
    "__future__",
)
ALLOWED_IMPORTS = ALLOWED_THIRD_PARTY + ALLOWED_STDLIB

# Data context accepted by run_python: DataFrames, file paths, or a mix keyed by name.
DataContext = Mapping[str, "pd.DataFrame | str | Path"] | DataSources


def _read_roots(work_dir: str) -> list[str]:
    """Directories the child may read from: the interpreter, libraries, fonts, tz data."""
    roots = {
        sys.prefix,
        sys.base_prefix,
        sys.exec_prefix,
        str(CHILD_DIR),
        "/usr/share/fonts",
        "/usr/local/share/fonts",
        "/usr/share/zoneinfo",
        "/etc/localtime",
        "/dev/null",
        "/dev/urandom",
        "/proc/self",
    }
    for key in ("stdlib", "platstdlib", "purelib", "platlib", "data"):
        if path := sysconfig.get_paths().get(key):
            roots.add(path)
    roots.update(site.getsitepackages())
    # Deliberately NOT the parent's sys.path / cwd: those can contain the project tree,
    # including uploaded datasets of other users.
    return sorted(roots)


@lru_cache(maxsize=1)
def _mpl_cache_dir() -> str | None:
    """A matplotlib config/font cache built once by a *trusted* process.

    Without it every sandbox run rebuilds the font cache (~3s). The sandbox may read it but
    not write it (only its own temp dir is writable), so one run cannot poison the next.
    """
    path = Path(tempfile.gettempdir()) / f"analyst-sandbox-mpl-{os.getuid()}"
    try:
        path.mkdir(mode=0o700, exist_ok=True)
        st = path.lstat()
        if path.is_symlink() or st.st_uid != os.getuid() or st.st_mode & 0o022:
            return None  # someone else controls it: don't trust it
        if not any(path.glob("fontlist-*.json")):
            subprocess.run(
                [
                    sys.executable,
                    "-s",
                    "-c",
                    "import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot",
                ],
                env={"MPLCONFIGDIR": str(path), "PATH": "/usr/bin:/bin", "HOME": str(path)},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
                check=False,
            )
    except (OSError, subprocess.SubprocessError):
        return None
    return str(path)


def _child_env(work_dir: str) -> dict[str, str]:
    mpl_dir = _mpl_cache_dir()
    read_roots = _read_roots(work_dir) + ([mpl_dir] if mpl_dir else [])
    cfg = {
        "work_dir": work_dir,
        "read_roots": read_roots,
        "allowed_imports": list(ALLOWED_IMPORTS),
    }
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": work_dir,
        "TMPDIR": work_dir,
        "LANG": "C.UTF-8",
        "PYTHONPATH": str(CHILD_DIR),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONHASHSEED": "0",
        "MPLBACKEND": "Agg",
        "MPLCONFIGDIR": mpl_dir or os.path.join(work_dir, ".mpl"),
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "ANALYST_SANDBOX_CONFIG": json.dumps(cfg),
    }


def _make_preexec(memory_mb: int, cpu_s: int):
    def _apply() -> None:  # runs in the child between fork and exec
        mem = memory_mb * 1024 * 1024
        limits = [
            (resource.RLIMIT_AS, mem),
            (resource.RLIMIT_CPU, cpu_s),
            (resource.RLIMIT_FSIZE, MAX_FILE_BYTES),
            (resource.RLIMIT_NOFILE, 256),
            (resource.RLIMIT_CORE, 0),
        ]
        for res, value in limits:
            resource.setrlimit(res, (value, value))

    return _apply


def _read_capped(path: Path) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as fh:
        data = fh.read(MAX_OUTPUT_BYTES + 1)
    text = data[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    if len(data) > MAX_OUTPUT_BYTES:
        text += f"\n[... output truncated at {MAX_OUTPUT_BYTES} bytes ...]"
    return text


def default_artifacts_dir() -> Path:
    """``AGENT_ARTIFACTS_DIR`` via the package's single ``Settings`` class (env or ``.env``)."""
    from analyst_agent.core.config import Settings  # lazy: core imports the sandbox

    return Settings().artifacts_dir


def _export_artifacts(work_dir: Path, artifacts_dir: Path) -> list[str]:
    """Copy files the code produced out of the temp dir; return their new paths.

    Open matplotlib figures (saved by the bootstrap) come first, then files the code wrote
    itself. Symlinks, oversized files and non-allowlisted types are skipped.
    """
    meta = work_dir / "__sandbox__"
    candidates = sorted((meta / "figures").glob("*.png"))
    candidates += sorted(
        p
        for p in work_dir.rglob("*")
        if meta not in p.parents and ".mpl" not in p.relative_to(work_dir).parts
    )
    run_id = uuid.uuid4().hex[:10]
    out: list[str] = []
    for p in candidates:
        if len(out) >= MAX_ARTIFACTS:
            break
        if p.is_symlink() or not p.is_file() or p.suffix.lower() not in ARTIFACT_SUFFIXES:
            continue
        if p.stat().st_size > MAX_FILE_BYTES:
            continue
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        dest = (
            artifacts_dir
            / f"py_{run_id}_{len(out):02d}_{safe_identifier(p.stem)}{p.suffix.lower()}"
        )
        dest.write_bytes(p.read_bytes())
        out.append(str(dest))
    return out


def _prepare_data(data_context: Any, data_dir: Path) -> None:
    if not data_context:
        return
    frames: dict[str, pd.DataFrame] = {}
    paths: dict[str, Any] = {}
    if isinstance(data_context, Mapping):
        for name, value in data_context.items():
            if isinstance(value, pd.DataFrame):
                frames[safe_identifier(str(name))] = value
            else:
                paths[str(name)] = value
        frames.update(load_dataframes(normalize_sources(paths)) if paths else {})
    else:
        frames.update(load_dataframes(normalize_sources(data_context)))
    data_dir.mkdir(parents=True)
    for name, df in frames.items():
        df.to_pickle(data_dir / f"{safe_identifier(name)}.pkl")


def run_python(
    code: str,
    data_context: DataContext | None = None,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    memory_limit_mb: int = DEFAULT_MEMORY_LIMIT_MB,
    artifacts_dir: str | Path | None = None,
) -> PyResult:
    """Execute ``code`` in a sandboxed subprocess and return a :class:`PyResult`.

    ``data_context`` maps names to DataFrames or data-file paths (CSV/Parquet/SQLite), or is
    a path / list of paths. Inside the code, tables are in ``tables[name]`` (and as variables
    of that name), a single table is also ``df``, and ``pd``/``np``/``plt``/``scipy`` are
    imported. Figures left open and files written to the cwd are copied to ``artifacts_dir``
    (default ``$AGENT_ARTIFACTS_DIR`` or ``artifacts/``) and listed in ``artifacts``.
    Never raises for user-code problems.
    """
    start = time.monotonic()
    artifacts_dir = Path(artifacts_dir) if artifacts_dir is not None else default_artifacts_dir()
    with tempfile.TemporaryDirectory(prefix="analyst-py-") as tmp:
        work_dir = Path(os.path.realpath(tmp))
        meta = work_dir / "__sandbox__"
        meta.mkdir()
        (meta / "code.py").write_text(code, encoding="utf-8")
        try:
            _prepare_data(data_context, meta / "data")
        except Exception as exc:  # noqa: BLE001 - surface as a structured error
            return _failure(T.DATA_LOAD_ERROR, f"{type(exc).__name__}: {exc}", start)

        stdout_path, stderr_path = meta / "stdout.txt", meta / "stderr.txt"
        timed_out = False
        with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
            proc = subprocess.Popen(
                [sys.executable, "-s", "-B", str(CHILD_DIR / "bootstrap.py"), str(work_dir)],
                cwd=work_dir,
                env=_child_env(str(work_dir)),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=True,
                close_fds=True,
                preexec_fn=_make_preexec(memory_limit_mb, int(timeout_s) + 5),
            )
            try:
                exit_code = proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                exit_code = proc.wait()

        stdout, stderr = _read_capped(stdout_path), _read_capped(stderr_path)
        payload: Any = None
        result_path = meta / "result.json"
        if result_path.is_file() and not result_path.is_symlink():
            try:
                payload = json.loads(result_path.read_text(encoding="utf-8")[: 1024 * 1024])
            except (ValueError, OSError):
                payload = None
        try:
            artifacts = _export_artifacts(work_dir, artifacts_dir)
        except OSError as exc:
            artifacts = []
            stderr += f"\n[sandbox] could not export artifacts: {exc}"

    base: dict[str, Any] = dict(stdout=stdout, stderr=stderr, artifacts=artifacts)

    if timed_out:
        return _failure(
            T.TIMEOUT,
            f"Execution exceeded the {timeout_s:g}s time limit and the process was killed.",
            start,
            **base,
        )
    if not isinstance(payload, dict):
        if exit_code in (-signal.SIGXCPU, -signal.SIGKILL):
            return _failure(
                T.TIMEOUT, "Process was killed for exceeding its CPU limit.", start, **base
            )
        if "MemoryError" in stderr:
            return _failure(
                T.MEMORY_LIMIT,
                f"Process ran out of memory (limit {memory_limit_mb} MB).",
                start,
                **base,
            )
        return _failure(
            T.PROCESS_CRASHED,
            f"Sandbox process exited with code {exit_code} without a result.",
            start,
            **base,
        )

    if not payload.get("ok"):
        e = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        kind, msg = str(e.get("type", "Error")), str(e.get("message", ""))
        if kind == T.MEMORY_LIMIT:
            msg = f"Out of memory (limit {memory_limit_mb} MB): {msg}"
        tb = e.get("traceback")
        if isinstance(tb, str) and tb:
            base["stderr"] = (stderr + ("\n" if stderr else "") + tb)[-MAX_OUTPUT_BYTES:]
        return _failure(kind, msg, start, **base)

    return PyResult(ok=True, duration_ms=_ms_since(start), **base)


def _ms_since(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _failure(kind: str, message: str, start: float, **kwargs: Any) -> PyResult:
    return PyResult(ok=False, error=T.fmt(kind, message), duration_ms=_ms_since(start), **kwargs)
