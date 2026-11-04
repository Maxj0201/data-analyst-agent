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
  ``CLONE_THREAD`` (``clone3`` gets ENOSYS so libc falls back to ``clone``), every
  ``socket`` (``AF_UNIX`` too), ``ptrace``/``process_vm_*``/``pidfd_*``/``kcmp``, signals to
  any other process, rlimit and scheduler changes, namespace/mount syscalls, and the
  capability-gated host-wide syscalls Landlock does not cover (``reboot``, ``init_module``,
  ``sethostname``, ``settimeofday``, ``syslog``, ...), chmod/chown/utimes/xattr changes and
  ``chattr``-style ioctls fail with EPERM; syscalls numbered 463 and up (newer than anything
  the interpreter uses) get ENOSYS; syscalls from a foreign ABI (i386 or x32 on x86_64) are
  refused. Applied to every thread (TSYNC). The full
  list is ``_DENIED`` / ``_SELF_ONLY`` below.

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
            "kill": 62,
            "tkill": 200,
            "tgkill": 234,
            "rt_sigqueueinfo": 129,
            "rt_tgsigqueueinfo": 297,
            "pidfd_send_signal": 424,
            "pidfd_open": 434,
            "pidfd_getfd": 438,
            "kcmp": 312,
            "setpriority": 141,
            "ioprio_set": 251,
            "sched_setparam": 142,
            "sched_setscheduler": 144,
            "sched_setaffinity": 203,
            "sched_setattr": 314,
            "init_module": 175,
            "finit_module": 313,
            "delete_module": 176,
            "kexec_load": 246,
            "kexec_file_load": 320,
            "reboot": 169,
            "mount": 165,
            "umount2": 166,
            "pivot_root": 155,
            "chroot": 161,
            "setns": 308,
            "swapon": 167,
            "swapoff": 168,
            "sethostname": 170,
            "setdomainname": 171,
            "settimeofday": 164,
            "clock_settime": 227,
            "clock_adjtime": 305,
            "adjtimex": 159,
            "iopl": 172,
            "ioperm": 173,
            "perf_event_open": 298,
            "userfaultfd": 323,
            "keyctl": 250,
            "add_key": 248,
            "request_key": 249,
            "open_by_handle_at": 304,
            "name_to_handle_at": 303,
            "fsopen": 430,
            "fsconfig": 431,
            "fsmount": 432,
            "fspick": 433,
            "move_mount": 429,
            "open_tree": 428,
            "mount_setattr": 442,
            "syslog": 103,
            "acct": 163,
            "quotactl": 179,
            "mknod": 133,
            "mknodat": 259,
            "lookup_dcookie": 212,
            "fanotify_init": 300,
            "vhangup": 153,
            "process_madvise": 440,
            "migrate_pages": 256,
            "move_pages": 279,
            "chmod": 90,
            "fchmod": 91,
            "fchmodat": 268,
            "fchmodat2": 452,
            "chown": 92,
            "fchown": 93,
            "lchown": 94,
            "fchownat": 260,
            "utime": 132,
            "utimes": 235,
            "futimesat": 261,
            "utimensat": 280,
            "setxattr": 188,
            "lsetxattr": 189,
            "fsetxattr": 190,
            "removexattr": 197,
            "lremovexattr": 198,
            "fremovexattr": 199,
            "ioctl": 16,
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
            "kill": 129,
            "tkill": 130,
            "tgkill": 131,
            "rt_sigqueueinfo": 138,
            "rt_tgsigqueueinfo": 240,
            "pidfd_send_signal": 424,
            "pidfd_open": 434,
            "pidfd_getfd": 438,
            "kcmp": 272,
            "setpriority": 140,
            "ioprio_set": 30,
            "sched_setparam": 118,
            "sched_setscheduler": 119,
            "sched_setaffinity": 122,
            "sched_setattr": 274,
            "init_module": 105,
            "finit_module": 273,
            "delete_module": 106,
            "kexec_load": 104,
            "kexec_file_load": 294,
            "reboot": 142,
            "mount": 40,
            "umount2": 39,
            "pivot_root": 41,
            "chroot": 51,
            "setns": 268,
            "swapon": 224,
            "swapoff": 225,
            "sethostname": 161,
            "setdomainname": 162,
            "settimeofday": 170,
            "clock_settime": 112,
            "clock_adjtime": 266,
            "adjtimex": 171,
            "perf_event_open": 241,
            "userfaultfd": 282,
            "keyctl": 219,
            "add_key": 217,
            "request_key": 218,
            "open_by_handle_at": 265,
            "name_to_handle_at": 264,
            "fsopen": 430,
            "fsconfig": 431,
            "fsmount": 432,
            "fspick": 433,
            "move_mount": 429,
            "open_tree": 428,
            "mount_setattr": 442,
            "syslog": 116,
            "acct": 89,
            "quotactl": 60,
            "mknodat": 33,
            "lookup_dcookie": 18,
            "fanotify_init": 262,
            "vhangup": 58,
            "process_madvise": 440,
            "migrate_pages": 238,
            "move_pages": 239,
            "fchmod": 52,
            "fchmodat": 53,
            "fchmodat2": 452,
            "fchown": 55,
            "fchownat": 54,
            "utimensat": 88,
            "setxattr": 5,
            "lsetxattr": 6,
            "fsetxattr": 7,
            "removexattr": 14,
            "lremovexattr": 15,
            "fremovexattr": 16,
            "ioctl": 29,
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
_X32_SYSCALL_BIT = 0x40000000

_DENIED = (
    # process creation and other processes' memory
    "execve",
    "execveat",
    "fork",
    "vfork",
    "ptrace",
    "process_vm_readv",
    "process_vm_writev",
    "kcmp",
    "pidfd_open",
    "pidfd_getfd",
    "pidfd_send_signal",
    "tkill",
    "process_madvise",
    "migrate_pages",
    "move_pages",
    # every socket, AF_UNIX included: Landlock (ABI <= 7) does not mediate connect() to a
    # pathname UNIX socket, e.g. /var/run/docker.sock (found by tests/sandbox/test_fuzz_escape.py)
    "socket",
    # resource limits and scheduling of this or other processes
    "setrlimit",
    "prlimit64",
    "setpriority",
    "ioprio_set",
    "sched_setparam",
    "sched_setscheduler",
    "sched_setaffinity",
    "sched_setattr",
    # namespaces, mounts, kernel objects
    "unshare",
    "setns",
    "mount",
    "umount2",
    "pivot_root",
    "chroot",
    "fsopen",
    "fsconfig",
    "fsmount",
    "fspick",
    "move_mount",
    "open_tree",
    "mount_setattr",
    "open_by_handle_at",
    "name_to_handle_at",
    "mknod",
    "mknodat",
    "bpf",
    "io_uring_setup",
    "perf_event_open",
    "userfaultfd",
    "keyctl",
    "add_key",
    "request_key",
    "fanotify_init",
    "lookup_dcookie",
    # host-wide state that needs a capability the service holds when it runs as root (the
    # production image does not, dev containers often do): Landlock covers none of these
    "init_module",
    "finit_module",
    "delete_module",
    "kexec_load",
    "kexec_file_load",
    "reboot",
    "swapon",
    "swapoff",
    "sethostname",
    "setdomainname",
    "settimeofday",
    "clock_settime",
    "clock_adjtime",
    "adjtimex",
    "syslog",
    "acct",
    "quotactl",
    "vhangup",
    "iopl",
    "ioperm",
    # metadata of files the process can merely open or name: Landlock (ABI <= 7) does not
    # mediate chmod/chown/utimes/xattrs, so these changed files the service user owns (as
    # root: any file), readable or not (found by tests/sandbox/test_fuzz_escape.py)
    "chmod",
    "fchmod",
    "fchmodat",
    "fchmodat2",
    "chown",
    "fchown",
    "lchown",
    "fchownat",
    "utime",
    "utimes",
    "futimesat",
    "utimensat",
    "setxattr",
    "lsetxattr",
    "fsetxattr",
    "removexattr",
    "lremovexattr",
    "fremovexattr",
)
# ioctl requests refused on any fd: chattr (+i/+a) and project/xflags changes on files the
# process can read, and pushing input into a terminal.
_IOCTL_DENIED = (
    0x40086602,  # FS_IOC_SETFLAGS
    0x40046602,  # FS_IOC32_SETFLAGS
    0x401C5820,  # FS_IOC_FSSETXATTR
    0x00005412,  # TIOCSTI
    0x0000541C,  # TIOCLINUX
)
# Syscalls numbered from here on are newer than anything the interpreter and its libraries use
# (glibc treats ENOSYS as "not supported" and falls back). Refusing them by default keeps
# future syscalls (setxattrat 463, file_setattr 469, ...) out until they are reviewed.
_FIRST_UNREVIEWED_NR = 463
# Signals: only to this process itself (raise(), pthread_kill(), os.kill(os.getpid(), ...)).
# Landlock ABI >= 6 scopes signals too; this also covers older kernels. args[0] is the pid/tgid.
_SELF_ONLY = ("kill", "tgkill", "rt_sigqueueinfo", "rt_tgsigqueueinfo")


class _Filter(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


def build_filter(machine: str | None = None, pid: int | None = None) -> bytes:
    """The seccomp-bpf program (raw ``sock_filter`` array) for ``machine``.

    ``pid`` (default: this process) is the only target allowed for the signal syscalls.
    """
    machine = machine or platform.machine()
    pid = os.getpid() if pid is None else pid
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
    op(_BPF_JGE, _FIRST_UNREVIEWED_NR, "enosys", None)
    for name in _DENIED:
        if name in nr:
            op(_BPF_JEQ, nr[name], "deny", None)
    op(_BPF_JEQ, nr["clone3"], "enosys", None)
    op(_BPF_JEQ, nr["clone"], "clone", None)
    for name in _SELF_ONLY:
        op(_BPF_JEQ, nr[name], "self", None)
    op(_BPF_JEQ, nr["ioctl"], "ioctl", None)
    op(_BPF_RET, _RET_ALLOW)
    label("ioctl")
    op(_BPF_LD_W_ABS, 24)  # low 32 bits of args[1] (request)
    for request in _IOCTL_DENIED:
        op(_BPF_JEQ, request, "deny", None)
    op(_BPF_RET, _RET_ALLOW)
    label("clone")
    op(_BPF_LD_W_ABS, 16)  # low 32 bits of args[0] (flags)
    op(_BPF_JSET, _CLONE_THREAD, "allow", "deny")
    label("self")
    op(_BPF_LD_W_ABS, 16)  # low 32 bits of args[0] (pid / tgid; pid_t is 32-bit)
    op(_BPF_JEQ, pid & 0xFFFFFFFF, "allow", "deny")
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


def confine(
    read_roots: Iterable[str],
    write_roots: Iterable[str],
    marks: dict[str, float] | None = None,
) -> list[str]:
    """Apply Landlock (best effort) and seccomp (required on Linux). Returns warnings.

    Raises :class:`ConfinementError` when seccomp cannot be installed on Linux, so the caller
    can refuse to run user code (fail closed). ``marks``, if given, receives
    ``time.perf_counter()`` timestamps after each step (used by scripts/bench_sandbox.py).
    """
    from time import perf_counter

    warnings: list[str] = []
    if not sys.platform.startswith("linux"):
        return [f"no kernel confinement on {sys.platform}; only the audit hook applies"]
    try:
        apply_landlock(list(read_roots), list(write_roots))
    except (OSError, ConfinementError) as exc:
        warnings.append(f"Landlock unavailable ({exc}); file access relies on the audit hook")
    if marks is not None:
        marks["landlock_done"] = perf_counter()
    try:
        apply_seccomp()
    except (OSError, ConfinementError) as exc:
        raise ConfinementError(f"could not install the seccomp filter: {exc}") from exc
    if marks is not None:
        marks["seccomp_done"] = perf_counter()
    return warnings
