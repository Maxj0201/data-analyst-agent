"""Child-process entry point for ``run_python``.

Invoked as ``python -s -B bootstrap.py <work_dir>`` with the working directory already set
to the per-run temp dir. Protocol (all files inside ``work_dir``):

* ``__sandbox__/code.py``   - user code (input)
* ``__sandbox__/data/*.pkl`` - pickled DataFrames written by the trusted parent (input)
* ``__sandbox__/result.json`` - structured outcome (output)
* ``__sandbox__/figures/*.png`` - figures left open by user code (output)

User code never raises out of this process: every exception is captured into result.json.
"""

import json
import linecache
import os
import sys
import traceback
from time import perf_counter

USER_CODE_FILENAME = "<analysis>"


def _write_result(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def main() -> int:
    work_dir = sys.argv[1]
    meta_dir = os.path.join(work_dir, "__sandbox__")
    result_path = os.path.join(meta_dir, "result.json")

    import builtins

    if not hasattr(builtins, "__sandbox_violation__"):  # fail closed
        _write_result(
            result_path,
            {
                "ok": False,
                "error": {
                    "type": "SandboxSetupError",
                    "message": "sandbox guard was not loaded; refusing to run",
                },
            },
        )
        return 3

    # Phase timestamps for scripts/bench_sandbox.py; only present when the parent asked.
    marks: dict | None = getattr(builtins, "__sandbox_marks__", None)

    def mark(name: str) -> None:
        if marks is not None:
            marks[name] = perf_counter()

    mark("boot_start")

    # ---- setup phase: trusted imports + data loading -----------------------------------
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    import pandas as pd
    import scipy

    mark("imports_done")
    data_dir = os.path.join(meta_dir, "data")
    tables: dict[str, pd.DataFrame] = {}
    if os.path.isdir(data_dir):
        for fname in sorted(os.listdir(data_dir)):
            if fname.endswith(".pkl"):
                tables[fname[:-4]] = pd.read_pickle(os.path.join(data_dir, fname))

    with open(os.path.join(meta_dir, "code.py"), encoding="utf-8") as fh:
        source = fh.read()
    mark("data_loaded")

    # Import allowlist for modules that are *already loaded* (the audit "import" event only
    # fires for first-time imports, and pandas/numpy pull in os, ctypes, subprocess, ...).
    allowed = frozenset(json.loads(os.environ["ANALYST_SANDBOX_CONFIG"])["allowed_imports"])
    real_import = builtins.__import__
    user_builtins = dict(vars(builtins))
    namespace: dict = {}

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
        if globals is namespace and level == 0 and name.split(".")[0] not in allowed:
            raise builtins.__sandbox_violation__(
                f"Blocked by sandbox: import of {name!r} (allowed: {', '.join(sorted(allowed))})"
            )
        return real_import(name, globals, locals, fromlist, level)

    user_builtins["__import__"] = guarded_import
    namespace.update(
        {
            "__name__": "__main__",
            "__builtins__": user_builtins,
            "pd": pd,
            "np": np,
            "plt": plt,
            "scipy": scipy,
            "tables": tables,
        }
    )
    for name, frame in tables.items():  # each table also available as a variable
        namespace.setdefault(name, frame)
    namespace["df"] = next(iter(tables.values())) if len(tables) == 1 else None

    linecache.cache[USER_CODE_FILENAME] = (
        len(source),
        None,
        source.splitlines(True),
        USER_CODE_FILENAME,
    )

    # ---- kernel confinement: Landlock + seccomp (see _oslock.py) ------------------------
    # The audit hook alone is bypassable from native code (``_posixsubprocess.fork_exec``,
    # pyarrow's own file I/O), so the kernel enforces the same boundary before user code.
    cfg = json.loads(os.environ["ANALYST_SANDBOX_CONFIG"])
    mark("confine_start")
    confine_warnings: list[str] = []
    try:
        import _oslock

        mark("oslock_imported")
        confine_warnings = _oslock.confine(cfg["read_roots"], [work_dir], marks=marks)
        for warning in confine_warnings:
            sys.stderr.write(f"[sandbox] {warning}\n")
    except Exception as exc:  # noqa: BLE001 - fail closed
        _write_result(
            result_path,
            {
                "ok": False,
                "error": {
                    "type": "SandboxSetupError",
                    "message": f"kernel confinement failed; refusing to run: {exc}",
                },
            },
        )
        return 3

    # ---- user phase: the audit hook becomes strict and cannot be relaxed -----------------
    sys.audit("sandbox.lock")

    payload: dict = {"ok": True, "error": None}
    mark("user_start")
    try:
        code = compile(source, USER_CODE_FILENAME, "exec")
        exec(code, namespace)
    except SystemExit as exc:
        if exc.code not in (None, 0):
            payload = {
                "ok": False,
                "error": {"type": "SystemExit", "message": f"code called exit({exc.code!r})"},
            }
    except BaseException as exc:  # noqa: BLE001 - report everything, raise nothing
        tb = traceback.TracebackException.from_exception(exc)
        tb.stack = traceback.StackSummary.from_list(
            [f for f in tb.stack if f.filename == USER_CODE_FILENAME] or list(tb.stack)[-3:]
        )
        etype = type(exc).__name__
        if etype == "SandboxViolation":
            etype = "PermissionDenied"
        elif isinstance(exc, MemoryError):
            etype = "MemoryLimitExceeded"
        payload = {
            "ok": False,
            "error": {
                "type": etype,
                "message": str(exc) or etype,
                "traceback": "".join(tb.format()),
            },
        }

    mark("user_end")
    try:
        fig_dir = os.path.join(meta_dir, "figures")
        os.makedirs(fig_dir, exist_ok=True)
        for i, num in enumerate(plt.get_fignums()):
            plt.figure(num).savefig(
                os.path.join(fig_dir, f"open_{i:03d}.png"), dpi=100, bbox_inches="tight"
            )
        plt.close("all")
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[sandbox] could not save figures: {exc}\n")

    mark("figures_done")
    # Also hand the warnings to the parent in the result file, so it can log them (stderr is
    # only returned to the model, never to an operator's log).
    payload["sandbox_warnings"] = confine_warnings
    if marks is not None:
        payload["profile"] = dict(marks)
    sys.stdout.flush()
    sys.stderr.flush()
    _write_result(result_path, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
