"""Sandbox guard, imported automatically by ``site`` at interpreter start-up.

This module runs inside the *child* interpreter before any user code. It:

* replaces the socket constructors so that no network socket can be created;
* installs a PEP 578 audit hook (which cannot be removed once installed) that denies
  network activity, process creation, signals to other processes, writes outside the
  sandbox working directory, reads outside an allowlist of directories, ctypes/FFI use
  and gc-based object discovery by user code, and imports from user code of modules
  outside the allowlist.

It is defense in depth on top of the OS-level controls applied by the parent
(separate process, rlimits, wall-clock kill, scrubbed environment, temp cwd). Python-level
hooks are *not* a complete security boundary; see docs/sandbox.md.
"""

import json
import os
import sys

_CONFIG_ENV = "ANALYST_SANDBOX_CONFIG"
USER_CODE_FILENAME = "<analysis>"


def _install() -> None:
    raw = os.environ.get(_CONFIG_ENV)
    if not raw:  # not running under the sandbox runner: do nothing
        return
    cfg = json.loads(raw)

    realpath = os.path.realpath
    fspath = os.fspath
    getframe = sys._getframe
    sep = os.sep

    work_dir = realpath(cfg["work_dir"])
    write_roots = tuple(realpath(p) for p in [work_dir])
    read_roots = tuple(realpath(p) for p in cfg["read_roots"]) + write_roots
    allowed_imports = frozenset(cfg["allowed_imports"])
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC

    # One-way state, only reachable via this closure. ``sandbox.lock`` audit event flips the
    # guard from "setup" (libraries are being imported by the trusted bootstrap) to "user".
    state = {"user_phase": False}

    class SandboxViolation(PermissionError):
        pass

    def _under(path, roots) -> bool:
        try:
            p = realpath(fspath(path))
        except (TypeError, ValueError):
            return False
        return any(p == r or p.startswith(r + sep) for r in roots)

    def _deny(what: str):
        raise SandboxViolation(f"Blocked by sandbox: {what}")

    def _is_user_import() -> bool:
        # Find the frame that triggered the import, skipping import machinery.
        f = getframe(2)
        while f is not None:
            fn = f.f_code.co_filename
            if fn.startswith("<frozen importlib") or fn.endswith(
                (f"importlib{sep}__init__.py", f"importlib{sep}_bootstrap.py")
            ):
                f = f.f_back
                continue
            return fn == USER_CODE_FILENAME
        return False

    deny_prefixes = (
        "socket.",
        "subprocess.",
        "os.exec",
        "os.spawn",
        "os.posix_spawn",
        "os.fork",
        "os.forkpty",
        "os.system",
        "os.kill",
        "os.killpg",
        "signal.pthread_kill",
        "pty.",
        "urllib.",
        "http.client.",
        "ftplib.",
        "smtplib.",
        "poplib.",
        "imaplib.",
        "nntplib.",
        "telnetlib.",
        "webbrowser.",
        "resource.setrlimit",
        "resource.prlimit",
        "os.setuid",
        "sys.remote_exec",
    )
    user_deny_prefixes = ("ctypes.", "gc.get_", "_posixsubprocess", "winreg.", "msvcrt.")
    two_path_events = {
        "os.rename",
        "os.link",
        "os.symlink",
        "shutil.copyfile",
        "shutil.copytree",
        "shutil.move",
        "shutil.copymode",
        "shutil.copystat",
    }
    one_path_write_events = {
        "os.remove",
        "os.rmdir",
        "os.mkdir",
        "os.chmod",
        "os.chown",
        "os.truncate",
        "os.utime",
        "os.mkfifo",
        "os.mknod",
        "shutil.rmtree",
        "shutil.chown",
        "shutil.make_archive",
        "shutil.unpack_archive",
        "os.removexattr",
        "os.setxattr",
        "os.chflags",
        "os.lchflags",
    }
    listing_events = {"os.listdir", "os.scandir", "os.walk", "glob.glob", "os.listxattr"}

    def hook(event: str, args: tuple) -> None:
        if event == "sandbox.lock":
            state["user_phase"] = True
            return
        if event.startswith(deny_prefixes):
            _deny(event)
        if event == "open":
            path, mode, flags = (tuple(args) + (None, None, None))[:3]
            if path is None or isinstance(path, int):
                return
            writing = False
            if isinstance(mode, str) and any(c in mode for c in "wax+"):
                writing = True
            if isinstance(flags, int) and flags > 0 and flags & write_flags:
                writing = True
            if writing:
                if not _under(path, write_roots):
                    _deny(f"write access to {path!r} (only the sandbox working dir is writable)")
            elif not _under(path, read_roots):
                _deny(f"read access to {path!r}")
            return
        if event in one_path_write_events:
            if not args or isinstance(args[0], int) or not _under(args[0], write_roots):
                _deny(f"{event} on {args[0] if args else '?'!r}")
            if (
                len(args) > 1
                and event in {"os.remove", "os.rmdir", "os.mkdir"}
                and args[-1] not in (None, -1)
            ):
                _deny(f"{event} with dir_fd")
            return
        if event in two_path_events:
            src, dst = args[0], args[1]
            if event in {"os.rename", "shutil.move", "os.link", "os.symlink"}:
                ok = _under(src, write_roots) and _under(dst, write_roots)
            else:
                ok = _under(src, read_roots) and _under(dst, write_roots)
            if not ok:
                _deny(f"{event} {src!r} -> {dst!r}")
            return
        if event in listing_events:
            target = args[0] if args else None
            if target is None or isinstance(target, int):
                return
            if event == "glob.glob":
                target = os.path.dirname(fspath(target)) or "."
            if not _under(target, read_roots):
                _deny(f"{event} on {target!r}")
            return
        if not state["user_phase"]:
            return
        if event.startswith(user_deny_prefixes):
            _deny(event)
        if event == "import":
            top = str(args[0]).split(".")[0]
            if top not in allowed_imports and _is_user_import():
                _deny(f"import of {args[0]!r} (allowed: {', '.join(sorted(allowed_imports))})")

    # OS resource limits, applied here (before the hook denies ``resource.setrlimit``) rather
    # than in a ``preexec_fn``: the parent may be a multi-threaded server, where running Python
    # between fork and exec can deadlock. Hard == soft, and seccomp later blocks raising them.
    import resource

    for name, value in (cfg.get("rlimits") or {}).items():
        res = getattr(resource, name)
        _, hard = resource.getrlimit(res)
        if hard != resource.RLIM_INFINITY:
            value = min(int(value), hard)
        resource.setrlimit(res, (int(value), int(value)))

    sys.addaudithook(hook)

    # Import-time network block (belt and braces with the audit hook above).
    import _socket
    import socket

    class _BlockedSocket(socket.socket):
        def __init__(self, *a, **k):
            _deny("network access (socket creation)")

    def _blocked(*a, **k):
        _deny("network access")

    socket.socket = _BlockedSocket
    socket.SocketType = _BlockedSocket
    _socket.socket = _BlockedSocket
    _socket.SocketType = _BlockedSocket
    for name in (
        "create_connection",
        "create_server",
        "getaddrinfo",
        "gethostbyname",
        "gethostbyname_ex",
        "gethostbyaddr",
        "socketpair",
        "fromfd",
    ):
        if hasattr(socket, name):
            setattr(socket, name, _blocked)
        if hasattr(_socket, name):
            setattr(_socket, name, _blocked)

    builtins_mod = sys.modules["builtins"]
    builtins_mod.__sandbox_violation__ = SandboxViolation  # used by the bootstrap for reporting


_install()
del _install
