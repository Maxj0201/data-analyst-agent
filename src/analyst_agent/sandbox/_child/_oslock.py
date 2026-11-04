"""Kernel-enforced confinement for the sandbox child (Linux only).

The audit hook in ``sitecustomize`` is a Python-level guard. Native code reachable from the
allowed libraries bypasses it: ``_posixsubprocess.fork_exec`` (reachable as an attribute of
already-imported modules) starts processes without an audit event, and pyarrow (loaded by
pandas) reads and writes files with its own C++ I/O. This module closes those holes in the
kernel, right before user code runs:

* **Landlock** (Linux >= 5.13): the process may only read files under the read roots, write
  under the work dir, execute nothing, and (ABI >= 4) make no TCP connections; with ABI >= 6
  it also cannot signal processes outside the sandbox. Unavailable kernels fall back to the
  audit hook with a warning.
* **seccomp-bpf**: ``execve``/``execveat``, ``fork``/``vfork``, ``clone`` without
  ``CLONE_THREAD`` (``clone3`` gets ENOSYS so libc falls back to ``clone``), sockets other
  than ``AF_UNIX``, ``ptrace``, ``process_vm_*``, ``setrlimit``/``prlimit64``, ``unshare``,
  ``bpf`` and ``io_uring_setup`` fail with EPERM; syscalls from a foreign ABI (i386 or x32 on
  x86_64) are refused. Applied to every thread (TSYNC).

Both are applied with ``no_new_privs`` set and cannot be undone by the process.
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import struct
import sys
from collections.abc import Iterable

# --------------------------------------------------------------------------- syscalls

_libc = ctypes.CDLL(None, use_errno=True)
_libc.syscall.restype = ctypes.c_long
_libc.prctl.restype = ctypes.c_int

PR_SET_NO_NEW_PRIVS = 38

_ARCH = {
    # machine: (AUDIT_ARCH_*, syscall numbers)
    "x86_64": (
        0xC000003E,
        {
            "execve": 59,
            "execveat": 322,
            "fork": 57,
            "vfork": 58,
            "clone": 56,
            "clone3": 435,
            "socket": 41,
            "ptrace": 101,
            "process_vm_readv": 310,
            "process_vm_writev": 311,
            "setrlimit": 160,
            "prlimit64": 302,
            "unshare": 272,
            "bpf": 321,
            "io_uring_setup": 425,
            "seccomp": 317,
        },
    ),
    "aarch64": (
        0xC00000B7,
        {
            "execve": 221,
            "execveat": 281,
            "clone": 220,
            "clone3": 435,
            "socket": 198,
            "ptrace": 117,
            "process_vm_readv": 270,
            "process_vm_writev": 271,
            "setrlimit": 164,
            "prlimit64": 261,
            "unshare": 97,
            "bpf": 280,
            "io_uring_setup": 425,
            "seccomp": 277,
        },
    ),
}
_ARCH["amd64"] = _ARCH["x86_64"]
_ARCH["arm64"] = _ARCH["aarch64"]

_SYS_LANDLOCK_CREATE_RULESET = 444  # same number on every architecture
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446


class ConfinementError(RuntimeError):
    pass


def _syscall(nr: int, *args: object) -> int:
    res = _libc.syscall(ctypes.c_long(nr), *args)
    if res < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return int(res)


def _no_new_privs() -> None:
    if _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


# --------------------------------------------------------------------------- Landlock

_FS_EXECUTE = 1 << 0
_FS_WRITE_FILE = 1 << 1
_FS_READ_FILE = 1 << 2
_FS_READ_DIR = 1 << 3
_FS_REMOVE_DIR = 1 << 4
_FS_REMOVE_FILE = 1 << 5
_FS_MAKE_CHAR = 1 << 6
_FS_MAKE_DIR = 1 << 7
_FS_MAKE_REG = 1 << 8
_FS_MAKE_SOCK = 1 << 9
_FS_MAKE_FIFO = 1 << 10
_FS_MAKE_BLOCK = 1 << 11
_FS_MAKE_SYM = 1 << 12
_FS_REFER = 1 << 13  # ABI 2
_FS_TRUNCATE = 1 << 14  # ABI 3
_FS_IOCTL_DEV = 1 << 15  # ABI 5
_NET_BIND_TCP = 1 << 0  # ABI 4
_NET_CONNECT_TCP = 1 << 1
_SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0  # ABI 6
_SCOPE_SIGNAL = 1 << 1

_FILE_ONLY_RIGHTS = _FS_EXECUTE | _FS_WRITE_FILE | _FS_READ_FILE | _FS_TRUNCATE | _FS_IOCTL_DEV
_READ = _FS_READ_FILE | _FS_READ_DIR
_WRITE = (
    _READ
    | _FS_WRITE_FILE
    | _FS_REMOVE_DIR
    | _FS_REMOVE_FILE
    | _FS_MAKE_DIR
    | _FS_MAKE_REG
    | _FS_MAKE_SOCK
    | _FS_MAKE_FIFO
    | _FS_MAKE_SYM
    | _FS_REFER
    | _FS_TRUNCATE
)
_DEVICE_RW = {"/dev/null"}


def landlock_abi() -> int:
    """Landlock ABI version of the running kernel (0 = unavailable)."""
    try:
        return _syscall(_SYS_LANDLOCK_CREATE_RULESET, None, ctypes.c_size_t(0), ctypes.c_uint32(1))
    except OSError:
        return 0


def _handled_fs(abi: int) -> int:
    handled = (1 << 13) - 1  # ABI 1: EXECUTE .. MAKE_SYM
    if abi >= 2:
        handled |= _FS_REFER
    if abi >= 3:
        handled |= _FS_TRUNCATE
    if abi >= 5:
        handled |= _FS_IOCTL_DEV
    return handled


def apply_landlock(read_roots: Iterable[str], write_roots: Iterable[str]) -> int:
    """Restrict filesystem (and TCP) access. Returns the ABI used; raises if unavailable."""
    abi = landlock_abi()
    if abi < 1:
        raise ConfinementError("Landlock is not supported by this kernel")
    handled_fs = _handled_fs(abi)
    fields = [handled_fs]
    if abi >= 4:
        fields.append(_NET_BIND_TCP | _NET_CONNECT_TCP)  # handled, never allowed
    if abi >= 6:
        fields.append(_SCOPE_ABSTRACT_UNIX_SOCKET | _SCOPE_SIGNAL)
    attr = struct.pack(f"={len(fields)}Q", *fields)
    buf = ctypes.create_string_buffer(attr, len(attr))
    ruleset = _syscall(_SYS_LANDLOCK_CREATE_RULESET, buf, ctypes.c_size_t(len(attr)), 0)
    try:
        rules: dict[str, int] = {}
        for path in read_roots:
            rules[path] = rules.get(path, 0) | _READ | (_FS_WRITE_FILE if path in _DEVICE_RW else 0)
        for path in write_roots:
            rules[path] = rules.get(path, 0) | _WRITE
        for path, access in rules.items():
            try:
                fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except OSError:
                continue  # missing root: nothing to allow
            try:
                if not os.path.isdir(path):
                    access &= _FILE_ONLY_RIGHTS
                access &= handled_fs
                if not access:
                    continue
                rule = ctypes.create_string_buffer(struct.pack("=Qi", access, fd), 12)
                _syscall(_SYS_LANDLOCK_ADD_RULE, ruleset, 1, rule, 0)  # PATH_BENEATH
            finally:
                os.close(fd)
        _no_new_privs()
        _syscall(_SYS_LANDLOCK_RESTRICT_SELF, ruleset, 0)
    finally:
        os.close(ruleset)
    return abi


# --------------------------------------------------------------------------- seccomp

_BPF_LD_W_ABS = 0x20
_BPF_JEQ = 0x15
_BPF_JGE = 0x35
_BPF_JSET = 0x45
_BPF_RET = 0x06
_RET_ALLOW = 0x7FFF0000
_RET_ERRNO = 0x00050000
_SECCOMP_SET_MODE_FILTER = 1
_SECCOMP_FILTER_FLAG_TSYNC = 1
_CLONE_THREAD = 0x00010000
_AF_UNIX = 1
_X32_SYSCALL_BIT = 0x40000000

_DENIED = (
    "execve",
    "execveat",
    "fork",
    "vfork",
    "ptrace",
    "process_vm_readv",
    "process_vm_writev",
    "setrlimit",
    "prlimit64",
    "unshare",
    "bpf",
    "io_uring_setup",
)


class _Filter(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


def build_filter(machine: str | None = None) -> bytes:
    """The seccomp-bpf program (raw ``sock_filter`` array) for ``machine``."""
    machine = machine or platform.machine()
    if machine not in _ARCH:
        raise ConfinementError(f"no seccomp filter for architecture {machine!r}")
    audit_arch, nr = _ARCH[machine]

    # (code, k, jt_label, jf_label); labels are resolved to relative offsets below.
    prog: list[tuple[int, int, str | None, str | None]] = []
    labels: dict[str, int] = {}

    def op(code: int, k: int, jt: str | None = None, jf: str | None = None) -> None:
        prog.append((code, k, jt, jf))

    def label(name: str) -> None:
        labels[name] = len(prog)

    op(_BPF_LD_W_ABS, 4)  # seccomp_data.arch
    op(_BPF_JEQ, audit_arch, None, "deny")
    op(_BPF_LD_W_ABS, 0)  # seccomp_data.nr
    if machine in ("x86_64", "amd64"):
        op(_BPF_JGE, _X32_SYSCALL_BIT, "deny", None)
    for name in _DENIED:
        if name in nr:
            op(_BPF_JEQ, nr[name], "deny", None)
    op(_BPF_JEQ, nr["clone3"], "enosys", None)
    op(_BPF_JEQ, nr["clone"], "clone", None)
    op(_BPF_JEQ, nr["socket"], "socket", None)
    op(_BPF_RET, _RET_ALLOW)
    label("clone")
    op(_BPF_LD_W_ABS, 16)  # low 32 bits of args[0] (flags)
    op(_BPF_JSET, _CLONE_THREAD, "allow", "deny")
    label("socket")
    op(_BPF_LD_W_ABS, 16)  # args[0] (domain)
    op(_BPF_JEQ, _AF_UNIX, "allow", "deny")
    label("allow")  # BPF only jumps forward, so the checks above share this return
    op(_BPF_RET, _RET_ALLOW)
    label("deny")
    op(_BPF_RET, _RET_ERRNO | errno.EPERM)
    label("enosys")
    op(_BPF_RET, _RET_ERRNO | errno.ENOSYS)

    out = bytearray()
    for i, (code, k, jt, jf) in enumerate(prog):
        jt_off = 0 if jt is None else labels[jt] - (i + 1)
        jf_off = 0 if jf is None else labels[jf] - (i + 1)
        if not (0 <= jt_off < 256 and 0 <= jf_off < 256):
            raise ConfinementError("seccomp jump out of range")
        out += struct.pack("=HBBI", code, jt_off, jf_off, k)
    return bytes(out)


def apply_seccomp(machine: str | None = None) -> None:
    machine = machine or platform.machine()
    program = build_filter(machine)
    buf = ctypes.create_string_buffer(program, len(program))
    fprog = _Filter(len(program) // 8, ctypes.cast(buf, ctypes.c_void_p))
    _no_new_privs()
    _syscall(
        _ARCH[machine][1]["seccomp"],
        _SECCOMP_SET_MODE_FILTER,
        _SECCOMP_FILTER_FLAG_TSYNC,
        ctypes.byref(fprog),
    )


# --------------------------------------------------------------------------- entry point


def confine(read_roots: Iterable[str], write_roots: Iterable[str]) -> list[str]:
    """Apply Landlock (best effort) and seccomp (required on Linux). Returns warnings.

    Raises :class:`ConfinementError` when seccomp cannot be installed on Linux, so the caller
    can refuse to run user code (fail closed).
    """
    warnings: list[str] = []
    if not sys.platform.startswith("linux"):
        return [f"no kernel confinement on {sys.platform}; only the audit hook applies"]
    try:
        apply_landlock(list(read_roots), list(write_roots))
    except (OSError, ConfinementError) as exc:
        warnings.append(f"Landlock unavailable ({exc}); file access relies on the audit hook")
    try:
        apply_seccomp()
    except (OSError, ConfinementError) as exc:
        raise ConfinementError(f"could not install the seccomp filter: {exc}") from exc
    return warnings
