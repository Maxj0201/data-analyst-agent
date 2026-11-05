"""Kernel layers only: apply the sandbox's Landlock + seccomp (``_oslock.confine``) to this
process, with no audit hook or import guard, then try what H1/H2 did. Run inside the container
(``docker exec -u app <ctr> python /harness/kernel_check.py``) so it sees the same kernel and
Docker seccomp profile as the API's sandbox children. Prints one JSON object.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import sysconfig
import tempfile
from pathlib import Path

import analyst_agent.sandbox as sandbox_pkg

sys.path.insert(0, str(Path(sandbox_pkg.__file__).parent / "_child"))
import _oslock  # noqa: E402

work = tempfile.mkdtemp()
read_roots = sorted({sys.base_prefix, sys.prefix, sysconfig.get_paths()["purelib"], work})
out: dict[str, object] = {"kernel": os.uname().release, "landlock_abi": _oslock.landlock_abi()}
out["warnings"] = _oslock.confine(read_roots, [work])


def attempt(name: str, fn) -> None:  # noqa: ANN001
    try:
        fn()
        out[name] = "allowed"
    except OSError as exc:
        out[name] = f"{type(exc).__name__}: {exc.strerror}"


def _write_inside() -> None:
    Path(work, "ok.txt").write_text("ok")


attempt("write_inside_workdir", _write_inside)
# H2: file access outside the roots. No audit hook here, so only Landlock can stop these.
attempt("read_etc_hostname", lambda: Path("/etc/hostname").read_bytes())
attempt("read_job_db", lambda: Path("/app/data/jobs.sqlite3").read_bytes())
attempt("write_data_volume", lambda: Path("/app/data/kernel_escaped").write_text("x"))
# H1: process creation (seccomp), and network (seccomp denies every socket()).
attempt("fork", lambda: os.waitpid(pid, 0) if (pid := os.fork()) else os._exit(0))
attempt("execv", lambda: os.execv("/bin/sh", ["/bin/sh", "-c", "true"]))
attempt("socket_inet", lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM))
attempt("socket_unix", lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
print(json.dumps(out))
