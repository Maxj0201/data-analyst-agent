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

    # ---- setup phase: trusted imports + data loading -----------------------------------
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    import pandas as pd
    import scipy

    data_dir = os.path.join(meta_dir, "data")
    tables: dict[str, pd.DataFrame] = {}
    if os.path.isdir(data_dir):
        for fname in sorted(os.listdir(data_dir)):
            if fname.endswith(".pkl"):
                tables[fname[:-4]] = pd.read_pickle(os.path.join(data_dir, fname))

    with open(os.path.join(meta_dir, "code.py"), encoding="utf-8") as fh:
        source = fh.read()

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

    # ---- user phase: the audit hook becomes strict and cannot be relaxed -----------------
    sys.audit("sandbox.lock")

    payload: dict = {"ok": True, "error": None}
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

    sys.stdout.flush()
    sys.stderr.flush()
    _write_result(result_path, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
