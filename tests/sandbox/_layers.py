"""Run sandboxed code with the Python-level guards removed, to test the kernel layer alone.

The audit hook, socket patch and import allowlist all live in the attacker's interpreter, and
docs/sandbox.md says they are not a security boundary. To check that seccomp, Landlock and the
rlimits hold *on their own*, ``kernel_only()`` puts a shim ``sitecustomize`` first on the
child's ``PYTHONPATH``. The shim applies the same rlimits and marks the guard as loaded (so the
bootstrap runs), but installs no audit hook and patches nothing. The bootstrap still applies
``_oslock.confine`` before user code, exactly as in production.

Nothing here changes production code: it wraps ``python_runner._child_env`` for the calling
thread only, so it is safe with a thread pool.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from analyst_agent.sandbox import python_runner

SHIM = """
import builtins, json, os, resource

_raw = os.environ.get("ANALYST_SANDBOX_CONFIG")
if _raw:
    for _name, _value in (json.loads(_raw).get("rlimits") or {}).items():
        _res = getattr(resource, _name)
        _hard = resource.getrlimit(_res)[1]
        if _hard != resource.RLIM_INFINITY:
            _value = min(int(_value), _hard)
        resource.setrlimit(_res, (int(_value), int(_value)))
    builtins.__sandbox_violation__ = PermissionError  # "guard loaded": bootstrap proceeds
"""

_local = threading.local()


def install(monkeypatch: pytest.MonkeyPatch, shim_dir: Path) -> None:
    """Wrap ``_child_env`` so that threads inside ``kernel_only()`` get the shim."""
    shim_dir.mkdir(parents=True, exist_ok=True)
    (shim_dir / "sitecustomize.py").write_text(SHIM)
    real = python_runner._child_env

    def child_env(work_dir: str, rlimits: dict[str, int] | None = None) -> dict[str, str]:
        env = real(work_dir, rlimits)
        if getattr(_local, "kernel_only", False):
            env["PYTHONPATH"] = f"{shim_dir}:{env['PYTHONPATH']}"
        return env

    monkeypatch.setattr(python_runner, "_child_env", child_env)


@contextmanager
def kernel_only(enabled: bool = True) -> Iterator[None]:
    prev = getattr(_local, "kernel_only", False)
    _local.kernel_only = enabled
    try:
        yield
    finally:
        _local.kernel_only = prev


# Prelude for kernel-only snippets: raw syscalls through ctypes (fine without the audit hook).
# The bootstrap's import allowlist (a guarded ``__import__`` in the user namespace) is still
# there, so modules come from the real ``__import__``.
CTYPES_PRELUDE = """
_imp = vars(pd)['__builtins__']['__import__']
ctypes, os = _imp('ctypes'), _imp('os')
_libc = ctypes.CDLL(None, use_errno=True)
_libc.syscall.restype = ctypes.c_long
def sc(nr, *args):
    ctypes.set_errno(0)
    r = _libc.syscall(ctypes.c_long(nr), *[ctypes.c_long(a) for a in args])
    return r, ctypes.get_errno()
"""
