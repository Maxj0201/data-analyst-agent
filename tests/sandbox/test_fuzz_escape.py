"""Grammar-based fuzzing of ``run_python`` with sandbox-escape attempts.

A seeded generator combines, for each snippet:

* an **attack** (``ATTACKS``): process execution, fork/thread bombs, file reads and writes
  outside the sandbox, network and UNIX-socket connections, signals to other processes,
  rlimit / scheduler changes, memory and disk exhaustion, pickle payloads, ``/proc`` and
  ``/dev`` tricks, pyarrow native I/O, and capability-gated syscalls (``reboot``,
  ``init_module``, ``mount``, ...);
* a **route to the module** it needs: ``import``, ``__import__``, the real ``__import__``
  stolen from ``vars(pd)['__builtins__']``, ``sys.modules`` reached through library internals
  (``pd.io.common.os.sys``), ``exec(compile(..., 'lib.py'), {})``, ``object.__subclasses__()``,
  ``np.__dict__['__builtins__']``;
* an **encoding of the module name** (literal, ``chr`` list, hex, reversed, rot13, base64 via
  ``codecs``, JSON ``\\u`` escapes, concatenation);
* a **path trick** for file targets (``/proc/self/root``, ``../`` runs, ``//``, bytes,
  ``pathlib``, symlink, hard link, ``dir_fd``, a ``__fspath__`` that changes its answer);
* a **prelude** that attacks the Python guards first (reset the audit hook's state from a
  traceback, monkeypatch ``posixpath._joinrealpath``, make ``isinstance`` lie);
* a **wrapper** (plain, function, swallowing ``try``, ``exec`` of a string, ``exec`` with
  fresh globals and a library-looking file name);
* a **mode**: ``full`` (production) or ``kernel`` (Python guards removed, see ``_layers.py``),
  so seccomp, Landlock and the rlimits are also tested on their own.

Every snippet must be **blocked** (no effect) or **killed within limits**. Oracles are
external wherever possible: marker files a spawned shell would create, a secret token that
must not appear in the output, canary files outside the sandbox that must stay unchanged,
TCP and UNIX listeners that must see no connection, and a victim process that must survive.
In-snippet checks print ``ESCAPED-<id>`` only after an attack *verifiably* worked (e.g. the
rlimit really changed, the syscall really reached the kernel).

Marked ``slow`` (about 5 minutes on 4 cores): CI runs it nightly, see
``.github/workflows/nightly.yml``. Knobs: ``SANDBOX_FUZZ_N`` (default 500),
``SANDBOX_FUZZ_SEED`` (default fixed, so a failure is reproducible),
``SANDBOX_FUZZ_WORKERS``, ``SANDBOX_FUZZ_REPORT`` (write a JSON report there).
Known gaps the fuzzer cannot close are listed in docs/sandbox.md.
"""

from __future__ import annotations

import base64
import codecs
import json
import os
import pickle
import platform
import random
import socket
import stat
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pytest

from analyst_agent.sandbox import python_runner

from . import _layers

N = int(os.environ.get("SANDBOX_FUZZ_N", "500"))
SEED = int(os.environ.get("SANDBOX_FUZZ_SEED", "20261104"))
WORKERS = int(os.environ.get("SANDBOX_FUZZ_WORKERS", str(min(8, os.cpu_count() or 2))))
TIMEOUT_S = 8.0
MEMORY_MB = 512
# Start-up is ~1.2 s on an idle machine; leave room for a loaded CI runner.
SLACK_S = 25.0
SECRET_PREFIX = "S3CR3T"

# Syscall numbers, kept here rather than taken from the module under test.
_SYSCALLS = {
    "x86_64": {
        "reboot": 169,
        "init_module": 175,
        "finit_module": 313,
        "delete_module": 176,
        "kexec_load": 246,
        "sethostname": 170,
        "settimeofday": 164,
        "clock_settime": 227,
        "mount": 165,
        "umount2": 166,
        "chroot": 161,
        "pivot_root": 155,
        "unshare": 272,
        "setns": 308,
        "syslog": 103,
        "perf_event_open": 298,
        "keyctl": 250,
        "bpf": 321,
        "io_uring_setup": 425,
        "userfaultfd": 323,
        "open_by_handle_at": 304,
        "mknodat": 259,
        "socket": 41,
        "ptrace": 101,
        "process_vm_readv": 310,
        "pidfd_open": 434,
        "kcmp": 312,
        "kill": 62,
        "tgkill": 234,
        "tkill": 200,
        "setpriority": 141,
        "sched_setaffinity": 203,
        "prlimit64": 302,
        "setrlimit": 160,
        "execve": 59,
        "fork": 57,
        "clone": 56,
        "clone3": 435,
        "iopl": 172,
        "acct": 163,
        "swapoff": 168,
        "setxattrat": 463,
    },
    "aarch64": {
        "reboot": 142,
        "init_module": 105,
        "finit_module": 273,
        "delete_module": 106,
        "kexec_load": 104,
        "sethostname": 161,
        "settimeofday": 170,
        "clock_settime": 112,
        "mount": 40,
        "umount2": 39,
        "chroot": 51,
        "pivot_root": 41,
        "unshare": 97,
        "setns": 268,
        "syslog": 116,
        "perf_event_open": 241,
        "keyctl": 219,
        "bpf": 280,
        "io_uring_setup": 425,
        "userfaultfd": 282,
        "mknodat": 33,
        "socket": 198,
        "ptrace": 117,
        "process_vm_readv": 270,
        "pidfd_open": 434,
        "kcmp": 272,
        "kill": 129,
        "tgkill": 131,
        "tkill": 130,
        "setpriority": 140,
        "sched_setaffinity": 122,
        "prlimit64": 261,
        "setrlimit": 164,
        "execve": 221,
        "clone": 220,
        "clone3": 435,
        "acct": 89,
        "swapoff": 225,
        "setxattrat": 463,
    },
}
NR = _SYSCALLS.get(
    {"amd64": "x86_64", "arm64": "aarch64"}.get(platform.machine(), platform.machine()), {}
)


# --------------------------------------------------------------------------- the grammar


@dataclass
class Ctx:
    """Everything a snippet may refer to. Paths are outside the sandbox unless noted."""

    case_id: int
    secret: str  # file holding the secret token
    out: str  # must never be created
    canary: str  # must stay byte-for-byte and mode-for-mode the same
    marker: str  # a spawned shell would create this
    poison: str  # inside the shared matplotlib cache dir: must never be created
    readable: str  # a file the sandbox may read (in a read root) but must not change
    port: int  # TCP listener
    unix: str  # UNIX socket listener
    victim: int  # pid of a process that must survive

    @property
    def token(self) -> str:
        return f"ESCAPED-{self.case_id}"


# Each attack: (category, name, body). The body is a template: ``MOD("x")`` is replaced by
# an expression returning module ``x`` (via the chosen route and name encoding), ``{PATH}``
# by the chosen path trick for the attack's target, and ``{ctx.*}`` fields are formatted in.
Attack = tuple[str, str, str]

_SH = "[b'/bin/sh', b'-c', b'touch {ctx.marker}']"
_SH_STR = "['/bin/sh', '-c', 'touch {ctx.marker}']"

EXEC: list[Attack] = [
    # system()/popen() report a failed spawn as an exit status, not an exception: for these
    # the marker file is the only oracle.
    ("exec", "os.system", "MOD(\"os\").system('touch {ctx.marker}')"),
    ("exec", "os.popen", "MOD(\"os\").popen('touch {ctx.marker}').read()"),
    ("exec", "subprocess.run", f"MOD(\"subprocess\").run({_SH_STR})\nprint('{{ctx.token}}')"),
    (
        "exec",
        "subprocess.Popen",
        f"MOD(\"subprocess\").Popen({_SH_STR}).wait()\nprint('{{ctx.token}}')",
    ),
    (
        "exec",
        "subprocess.check_output",
        f"MOD(\"subprocess\").check_output({_SH_STR})\nprint('{{ctx.token}}')",
    ),
    (
        "exec",
        "os.posix_spawn",
        f"_o = MOD(\"os\")\n_p = _o.posix_spawn('/bin/sh', {_SH_STR}, {{{{}}}})\n"
        "_o.waitpid(_p, 0)\nprint('{ctx.token}')",
    ),
    (
        "exec",
        "os.spawnv",
        f"_o = MOD(\"os\")\n_o.spawnv(_o.P_WAIT, '/bin/sh', {_SH_STR})\nprint('{{ctx.token}}')",
    ),
    ("exec", "os.execv", f"MOD(\"os\").execv('/bin/sh', {_SH_STR})"),
    (
        "exec",
        "fork+execv",
        f"_o = MOD(\"os\")\n_p = _o.fork()\nif _p == 0:\n    _o.execv('/bin/sh', {_SH_STR})\n"
        "_o.waitpid(_p, 0)\nprint('{ctx.token}')",
    ),
    (
        "exec",
        "_posixsubprocess.fork_exec",
        '_o = MOD("os")\n_m = MOD("_posixsubprocess")\n_r, _w = _o.pipe()\n_er, _ew = _o.pipe()\n'
        f"_a = [{_SH}, [b'/bin/sh'], True, (), None, None, -1, -1, -1, _w, -1, _w, _er, _ew,"
        " False, False, -1, None, None, None, -1, None]\n"
        "try:\n    _m.fork_exec(*_a, False)\nexcept TypeError:\n    _m.fork_exec(*_a)\n"
        "_o.read(_er, 100)\nprint('{ctx.token}')",
    ),
    ("exec", "pty.spawn", f"MOD(\"pty\").spawn({_SH_STR})\nprint('{{ctx.token}}')"),
    (
        "exec",
        "ctypes system()",
        "MOD(\"ctypes\").CDLL(None).system(b'touch {ctx.marker}')",
    ),
    (
        "exec",
        "multiprocessing",
        '_mp = MOD("multiprocessing")\n_p = _mp.Process(target=print)\n_p.start()\n_p.join()\n'
        "print('{ctx.token}')",
    ),
]

FORK: list[Attack] = [
    (
        "fork_bomb",
        "os.fork loop",
        '_o = MOD("os")\n_n = 0\nfor _ in range(200):\n    try:\n        _p = _o.fork()\n'
        "    except Exception:\n        continue\n"
        "    if _p == 0:\n        _o._exit(0)\n    _n += 1\n"
        "if _n:\n    print('{ctx.token}')",
    ),
    ("fork_bomb", "os.fork forever", '_o = MOD("os")\nwhile True:\n    _o.fork()'),
    (
        "fork_bomb",
        "fork forever, swallowing errors",
        '_o = MOD("os")\nwhile True:\n    try:\n        _o.fork()\n    except BaseException:\n'
        "        pass",
    ),
    (
        "fork_bomb",
        "thread bomb",
        '_t = MOD("_thread")\n_tm = MOD("time")\nwhile True:\n'
        "    _t.start_new_thread(_tm.sleep, (60,))",
    ),
    (
        "fork_bomb",
        "posix_spawn loop",
        f'_o = MOD("os")\nfor _ in range(100):\n'
        f"    _o.posix_spawn('/bin/sh', {_SH_STR}, {{{{}}}})\n"
        "print('{ctx.token}')",
    ),
    (
        "fork_bomb",
        "multiprocessing pool",
        "_mp = MOD(\"multiprocessing\")\n_mp.Pool(64).map(abs, range(64))\nprint('{ctx.token}')",
    ),
]

# File reads: the target path is {PATH}; success is checked against the secret prefix.
_READ_OK = f"\nif '{SECRET_PREFIX}' in repr(_d):\n    print('{{ctx.token}}')"
READ: list[Attack] = [
    ("file_read", "open", "_d = open({PATH}).read()" + _READ_OK),
    ("file_read", "io.open rb", "_d = MOD(\"io\").open({PATH}, 'rb').read()" + _READ_OK),
    (
        "file_read",
        "os.open + os.read",
        '_o = MOD("os")\n_d = _o.read(_o.open({PATH}, _o.O_RDONLY), 4096)' + _READ_OK,
    ),
    ("file_read", "pd.read_csv", "_d = pd.read_csv({PATH}, header=None).to_string()" + _READ_OK),
    (
        "file_read",
        "pd.read_table",
        "_d = pd.read_table({PATH}, header=None).to_string()" + _READ_OK,
    ),
    ("file_read", "np.fromfile", "_d = np.fromfile({PATH}, dtype='u1').tobytes()" + _READ_OK),
    (
        "file_read",
        "np.loadtxt str",
        "_d = np.loadtxt({PATH}, dtype=str, delimiter='\\x00')" + _READ_OK,
    ),
    (
        "file_read",
        "pyarrow.input_stream",
        '_d = MOD("pyarrow").input_stream({PATH}).read()' + _READ_OK,
    ),
    ("file_read", "pyarrow.OSFile", '_d = MOD("pyarrow").OSFile({PATH}).read()' + _READ_OK),
    (
        "file_read",
        "pyarrow.fs.LocalFileSystem",
        '_fs = MOD("pyarrow.fs").LocalFileSystem()\n'
        "_d = _fs.open_input_stream(str({PATH})).read()" + _READ_OK,
    ),
    (
        "file_read",
        "pyarrow.csv.read_csv",
        '_d = MOD("pyarrow.csv").read_csv({PATH}).to_pandas().to_string()' + _READ_OK,
    ),
    ("file_read", "linecache", '_d = MOD("linecache").getline(str({PATH}), 1)' + _READ_OK),
    (
        "file_read",
        "mmap",
        '_o = MOD("os")\n_fd = _o.open({PATH}, _o.O_RDONLY)\n'
        '_d = MOD("mmap").mmap(_fd, 0, prot=1)[:]' + _READ_OK,
    ),
    (
        "file_read",
        "shutil.copyfile into cwd",
        "MOD(\"shutil\").copyfile({PATH}, 'copy.txt')\n_d = open('copy.txt').read()" + _READ_OK,
    ),
    ("file_read", "codecs.open", '_d = MOD("codecs").open({PATH}).read()' + _READ_OK),
    (
        "file_read",
        "pd.io.common.get_handle",
        "_d = pd.io.common.get_handle({PATH}, 'r').handle.read()" + _READ_OK,
    ),
    (
        "file_read",
        "matplotlib.cbook",
        '_d = MOD("matplotlib.cbook").to_filehandle({PATH}).read()' + _READ_OK,
    ),
    (
        "file_read",
        "os.sendfile to stdout",
        '_o = MOD("os")\n_fd = _o.open({PATH}, _o.O_RDONLY)\n_o.sendfile(1, _fd, 0, 4096)',
    ),
]

PROC_READ: list[Attack] = [
    (
        "proc",
        "/proc/<ppid>/environ",
        "_o = MOD(\"os\")\n_d = open(f'/proc/{{_o.getppid()}}/environ', 'rb').read()\n"
        "if _d:\n    print('{ctx.token}')",
    ),
    (
        "proc",
        "/proc/1/environ",
        "_d = open('/proc/1/environ', 'rb').read()\nif _d:\n    print('{ctx.token}')",
    ),
    (
        "proc",
        "/proc/<ppid>/cmdline via pyarrow",
        '_o = MOD("os")\n_d = MOD("pyarrow").input_stream(f\'/proc/{{_o.getppid()}}/cmdline\')'
        ".read()\nif _d:\n    print('{ctx.token}')",
    ),
    (
        "proc",
        "/proc/<ppid>/mem",
        "_o = MOD(\"os\")\n_fd = _o.open(f'/proc/{{_o.getppid()}}/mem', _o.O_RDONLY)\n"
        "print('{ctx.token}')",
    ),
    (
        "proc",
        "/proc/<ppid>/cwd listing",
        "_o = MOD(\"os\")\n_d = _o.listdir(f'/proc/{{_o.getppid()}}/cwd')\nprint('{ctx.token}')",
    ),
    (
        "proc",
        "/proc/<ppid>/fd/0",
        "_o = MOD(\"os\")\n_fd = _o.open(f'/proc/{{_o.getppid()}}/fd/0', _o.O_RDONLY)\n"
        "print('{ctx.token}')",
    ),
    (
        "proc",
        "/proc/self/mem write",
        "_o = MOD(\"os\")\n_fd = _o.open('/proc/self/mem', _o.O_RDWR)\nprint('{ctx.token}')",
    ),
    (
        "proc",
        "/proc/sysrq-trigger open for write",
        "_o = MOD(\"os\")\n_fd = _o.open('/proc/sysrq-trigger', _o.O_WRONLY)\nprint('{ctx.token}')",
    ),
    (
        "proc",
        "/proc/sys/kernel/hostname open for write",
        "_o = MOD(\"os\")\n_fd = _o.open('/proc/sys/kernel/hostname', _o.O_WRONLY)\n"
        "print('{ctx.token}')",
    ),
    (
        "proc",
        "/sys listing",
        "_d = MOD(\"os\").listdir('/sys/class/net')\nprint('{ctx.token}')",
    ),
    (
        "proc",
        "/dev/kmsg",
        "_o = MOD(\"os\")\n_fd = _o.open('/dev/kmsg', _o.O_RDONLY | _o.O_NONBLOCK)\n"
        "print('{ctx.token}')",
    ),
    (
        "proc",
        "/dev/mem",
        "_o = MOD(\"os\")\n_fd = _o.open('/dev/mem', _o.O_RDONLY)\nprint('{ctx.token}')",
    ),
    (
        "proc",
        "/proc/self/root traversal",
        "_d = open('/proc/self/root{ctx.secret}').read()" + _READ_OK,
    ),
    (
        "proc",
        "/proc/self/cwd/.. traversal",
        "_d = open('/proc/self/cwd/' + '../' * 30 + '{ctx.secret}'.lstrip('/')).read()" + _READ_OK,
    ),
]

# Writes: the target is {PATH} (ctx.out, or a file in the shared matplotlib cache).
WRITE: list[Attack] = [
    ("file_write", "open w", "open({PATH}, 'w').write('x')"),
    (
        "file_write",
        "os.open O_CREAT",
        "_o = MOD(\"os\")\n_o.write(_o.open({PATH}, _o.O_WRONLY | _o.O_CREAT, 0o644), b'x')",
    ),
    ("file_write", "to_csv", "pd.DataFrame({{'a': [1]}}).to_csv({PATH})"),
    ("file_write", "to_parquet (pyarrow)", "pd.DataFrame({{'a': [1]}}).to_parquet({PATH})"),
    ("file_write", "to_pickle", "pd.DataFrame({{'a': [1]}}).to_pickle({PATH})"),
    ("file_write", "to_json", "pd.DataFrame({{'a': [1]}}).to_json({PATH})"),
    ("file_write", "np.save", "np.save({PATH}, np.arange(3))"),
    ("file_write", "ndarray.tofile", "np.arange(3).tofile({PATH})"),
    ("file_write", "plt.savefig", "plt.plot([1, 2])\nplt.savefig({PATH}, format='png')"),
    (
        "file_write",
        "pyarrow.output_stream",
        "_s = MOD(\"pyarrow\").output_stream({PATH})\n_s.write(b'x')\n_s.close()",
    ),
    (
        "file_write",
        "pyarrow.fs output",
        '_fs = MOD("pyarrow.fs").LocalFileSystem()\n'
        "_s = _fs.open_output_stream(str({PATH}))\n_s.write(b'x')\n_s.close()",
    ),
    (
        "file_write",
        "shutil.copy",
        "open('w.txt', 'w').write('x')\nMOD(\"shutil\").copy('w.txt', {PATH})",
    ),
    (
        "file_write",
        "os.rename out of cwd",
        "open('w.txt', 'w').write('x')\nMOD(\"os\").rename('w.txt', {PATH})",
    ),
    (
        "file_write",
        "os.link out of cwd",
        "open('w.txt', 'w').write('x')\nMOD(\"os\").link('w.txt', {PATH})",
    ),
    (
        "file_write",
        "symlink then write",
        "MOD(\"os\").symlink({PATH}, 'l')\nopen('l', 'w').write('x')",
    ),
    ("file_write", "os.mkdir", 'MOD("os").mkdir({PATH})'),
    ("file_write", "pathlib write_text", "MOD(\"pathlib\").Path({PATH}).write_text('x')"),
    (
        "file_write",
        "sqlite3",
        "MOD(\"sqlite3\").connect(str({PATH})).execute('create table t(x)')",
    ),
    ("file_write", "np.savetxt", "np.savetxt({PATH}, np.arange(3))"),
    (
        "file_write",
        "zipfile",
        "_z = MOD(\"zipfile\").ZipFile({PATH}, 'w')\n_z.writestr('a', 'x')\n_z.close()",
    ),
]

# Modifications of an existing file outside the sandbox (the canary).
CANARY: list[Attack] = [
    ("canary", "os.remove", "MOD(\"os\").remove('{ctx.canary}')"),
    ("canary", "os.unlink via pathlib", "MOD(\"pathlib\").Path('{ctx.canary}').unlink()"),
    ("canary", "os.truncate", "MOD(\"os\").truncate('{ctx.canary}', 0)"),
    ("canary", "open r+ write", "_f = open('{ctx.canary}', 'r+')\n_f.write('PWNED')\n_f.close()"),
    ("canary", "os.chmod", "MOD(\"os\").chmod('{ctx.canary}', 0o777)"),
    ("canary", "os.utime", "MOD(\"os\").utime('{ctx.canary}', (0, 0))"),
    ("canary", "os.chown", "MOD(\"os\").chown('{ctx.canary}', 1, 1)"),
    ("canary", "os.setxattr", "MOD(\"os\").setxattr('{ctx.canary}', 'user.pwned', b'1')"),
    ("canary", "os.rename into cwd", "MOD(\"os\").rename('{ctx.canary}', 'stolen.txt')"),
    ("canary", "shutil.move into cwd", "MOD(\"shutil\").move('{ctx.canary}', 'stolen.txt')"),
    (
        "canary",
        "shutil.rmtree parent",
        'MOD("shutil").rmtree(MOD("os").path.dirname(\'{ctx.canary}\'))',
    ),
    (
        "canary",
        "pyarrow overwrite",
        "_s = MOD(\"pyarrow\").output_stream('{ctx.canary}')\n_s.write(b'PWNED')\n_s.close()",
    ),
]

# Metadata of a file the sandbox can open for reading (it lives in a read root). Landlock
# grants the read; nothing may change the file through the fd or its path.
_RO = "_o = MOD(\"os\")\n_fd = _o.open('{ctx.readable}', _o.O_RDONLY)\n"
METADATA: list[Attack] = [
    ("metadata", "chmod path", "MOD(\"os\").chmod('{ctx.readable}', 0o777)"),
    ("metadata", "fchmod read-only fd", _RO + "_o.fchmod(_fd, 0o777)"),
    ("metadata", "chown path", "MOD(\"os\").chown('{ctx.readable}', 1, 1)"),
    ("metadata", "fchown read-only fd", _RO + "_o.fchown(_fd, 1, 1)"),
    ("metadata", "utime path", "MOD(\"os\").utime('{ctx.readable}', (0, 0))"),
    ("metadata", "futimens read-only fd", _RO + "_o.utime(_fd, (0, 0))"),
    ("metadata", "setxattr path", "MOD(\"os\").setxattr('{ctx.readable}', 'user.p', b'1')"),
    ("metadata", "fsetxattr read-only fd", _RO + "_o.setxattr(_fd, 'user.p', b'1')"),
    (
        "metadata",
        "chattr via FS_IOC_SETFLAGS",
        _RO + '_fc = MOD("fcntl")\n_b = bytearray(8)\n_fc.ioctl(_fd, 0x80086601, _b)\n'
        "_fc.ioctl(_fd, 0x40086602, _b)\nprint('{ctx.token}')",
    ),
    ("metadata", "write through read-only fd reopened", _RO + "open(_fd, 'w').write('x')"),
    (
        "metadata",
        "/proc/self/fd reopen for write",
        _RO + "open(f'/proc/self/fd/{{_fd}}', 'w').write('x')",
    ),
]

NET: list[Attack] = [
    (
        "network",
        "socket.connect",
        '_s = MOD("socket")\n_c = _s.socket(_s.AF_INET, _s.SOCK_STREAM)\n'
        "_c.connect(('127.0.0.1', {ctx.port}))\nprint('{ctx.token}')",
    ),
    (
        "network",
        "create_connection",
        "MOD(\"socket\").create_connection(('127.0.0.1', {ctx.port}), timeout=2)\n"
        "print('{ctx.token}')",
    ),
    (
        "network",
        "original _socket class via __mro__",
        '_s = MOD("socket")\n'
        "_base = [c for c in _s.socket.__mro__ if c.__module__ == '_socket'][0]\n"
        "_c = _base(_s.AF_INET, _s.SOCK_STREAM)\n_c.connect(('127.0.0.1', {ctx.port}))\n"
        "print('{ctx.token}')",
    ),
    (
        "network",
        "urllib",
        "MOD(\"urllib.request\").urlopen('http://127.0.0.1:{ctx.port}/', timeout=2)\n"
        "print('{ctx.token}')",
    ),
    (
        "network",
        "http.client",
        "_h = MOD(\"http.client\").HTTPConnection('127.0.0.1', {ctx.port}, timeout=2)\n"
        "_h.request('GET', '/')\nprint('{ctx.token}')",
    ),
    (
        "network",
        "pd.read_csv(http)",
        "pd.read_csv('http://127.0.0.1:{ctx.port}/x.csv')\nprint('{ctx.token}')",
    ),
    (
        "network",
        "smtplib",
        "MOD(\"smtplib\").SMTP('127.0.0.1', {ctx.port}, timeout=2)\nprint('{ctx.token}')",
    ),
    (
        "network",
        "UDP sendto",
        '_s = MOD("socket")\n_c = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)\n'
        "_c.sendto(b'x', ('127.0.0.1', {ctx.port}))\nprint('{ctx.token}')",
    ),
    (
        "network",
        "AF_UNIX connect (socket module)",
        '_s = MOD("socket")\n_c = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)\n'
        "_c.connect('{ctx.unix}')\nprint('{ctx.token}')",
    ),
    (
        "network",
        "AF_UNIX via multiprocessing.connection",
        "MOD(\"multiprocessing.connection\").Client('{ctx.unix}')\nprint('{ctx.token}')",
    ),
    (
        "network",
        "AF_UNIX via original _socket class",
        '_s = MOD("socket")\n'
        "_base = [c for c in _s.socket.__mro__ if c.__module__ == '_socket'][0]\n"
        "_c = _base(_s.AF_UNIX, _s.SOCK_STREAM)\n_c.connect('{ctx.unix}')\nprint('{ctx.token}')",
    ),
    (
        "network",
        "socketpair is not an escape, socket() after it is",
        '_s = MOD("socket")\n_a, _b = _s.socketpair()\n_c = _s.socket(_s.AF_UNIX)\n'
        "_c.connect('{ctx.unix}')\nprint('{ctx.token}')",
    ),
]

SIGNALS: list[Attack] = [
    ("signal", "os.kill victim", "MOD(\"os\").kill({ctx.victim}, 15)\nprint('{ctx.token}')"),
    (
        "signal",
        "os.killpg victim",
        "_o = MOD(\"os\")\n_o.killpg(_o.getpgid({ctx.victim}), 15)\nprint('{ctx.token}')",
    ),
    (
        "signal",
        "pidfd_send_signal victim",
        '_o = MOD("os")\nMOD("signal").pidfd_send_signal(_o.pidfd_open({ctx.victim}), 15)\n'
        "print('{ctx.token}')",
    ),
    (
        "signal",
        "probe parent with signal 0",
        "_o = MOD(\"os\")\n_o.kill(_o.getppid(), 0)\nprint('{ctx.token}')",
    ),
    ("signal", "probe pid 1 with signal 0", "MOD(\"os\").kill(1, 0)\nprint('{ctx.token}')"),
    (
        "signal",
        "probe every process (-1) with signal 0",
        "MOD(\"os\").kill(-1, 0)\nprint('{ctx.token}')",
    ),
    (
        "signal",
        "ignore SIGXCPU + spin",
        '_g = MOD("signal")\n_g.signal(_g.SIGXCPU, _g.SIG_IGN)\nwhile True:\n    pass',
    ),
    (
        "signal",
        "ignore SIGTERM/SIGINT + spin",
        '_g = MOD("signal")\nfor _s in (_g.SIGTERM, _g.SIGINT, _g.SIGHUP):\n'
        "    _g.signal(_s, _g.SIG_IGN)\nwhile True:\n    pass",
    ),
    (
        "signal",
        "block all signals + spin",
        '_g = MOD("signal")\n_g.pthread_sigmask(_g.SIG_BLOCK, range(1, 32))\nwhile True:\n    pass',
    ),
]

RLIMIT: list[Attack] = [
    *[
        (
            "rlimit",
            f"setrlimit {r} unlimited",
            f'_r = MOD("resource")\n_r.setrlimit(_r.{r}, (_r.RLIM_INFINITY, _r.RLIM_INFINITY))\n'
            f"if _r.getrlimit(_r.{r})[0] == _r.RLIM_INFINITY:\n    print('{{ctx.token}}')",
        )
        for r in ("RLIMIT_AS", "RLIMIT_CPU", "RLIMIT_FSIZE", "RLIMIT_NOFILE", "RLIMIT_CORE")
    ],
    (
        "rlimit",
        "prlimit self",
        '_r = MOD("resource")\n_r.prlimit(0, _r.RLIMIT_AS, (_r.RLIM_INFINITY, _r.RLIM_INFINITY))\n'
        "print('{ctx.token}')",
    ),
    (
        "rlimit",
        "prlimit read parent",
        '_r = MOD("resource")\n_r.prlimit(MOD("os").getppid(), _r.RLIMIT_NOFILE)\n'
        "print('{ctx.token}')",
    ),
    (
        "rlimit",
        "setpriority victim",
        "_o = MOD(\"os\")\n_o.setpriority(_o.PRIO_PROCESS, {ctx.victim}, 19)\nprint('{ctx.token}')",
    ),
    (
        "rlimit",
        "raise own priority",
        "_o = MOD(\"os\")\n_o.setpriority(_o.PRIO_PROCESS, 0, -20)\nprint('{ctx.token}')",
    ),
    (
        "rlimit",
        "sched_setaffinity victim",
        "MOD(\"os\").sched_setaffinity({ctx.victim}, {{0}})\nprint('{ctx.token}')",
    ),
    (
        "rlimit",
        "realtime scheduling",
        '_o = MOD("os")\n_o.sched_setscheduler(0, _o.SCHED_FIFO, _o.sched_param(1))\n'
        "print('{ctx.token}')",
    ),
]

EXHAUST: list[Attack] = [
    ("memory", "huge string", "_x = 'x' * (1 << 34)\nprint('{ctx.token}')"),
    ("memory", "huge ndarray", "_x = np.ones(1 << 31)\nprint('{ctx.token}')"),
    ("memory", "growing list", "_l = []\nwhile True:\n    _l.append(bytearray(1 << 20))"),
    (
        "memory",
        "huge DataFrame",
        "_x = pd.DataFrame(np.zeros((1 << 16, 1 << 14)))\nprint('{ctx.token}')",
    ),
    (
        "memory",
        "deep recursion",
        'MOD("sys").setrecursionlimit(10 ** 8)\ndef _f(n):\n    return _f(n + 1)\n_f(0)',
    ),
    (
        "disk",
        "file over RLIMIT_FSIZE",
        "_f = open('big.bin', 'wb')\nfor _ in range(100):\n    _f.write(b'x' * (1 << 20))\n"
        "_f.close()\n"
        "if MOD(\"os\").path.getsize('big.bin') > 64 * (1 << 20):\n    print('{ctx.token}')",
    ),
    ("cpu", "busy loop", "while True:\n    pass"),
    (
        "cpu",
        "busy loop, swallowing everything",
        "def _f():\n    while True:\n        try:\n            while True:\n                pass\n"
        "        except BaseException:\n            pass\n_f()",
    ),
]


def _pickle_payloads(ctx: Ctx) -> list[Attack]:
    class _Sys:
        def __reduce__(self) -> tuple:
            return (os.system, (f"touch {ctx.marker}",))

    class _Spawn:
        def __reduce__(self) -> tuple:
            return (subprocess.check_call, (["/bin/sh", "-c", f"touch {ctx.marker}"],))

    class _Exec:
        def __reduce__(self) -> tuple:
            return (exec, (f"__import__('os').system('touch {ctx.marker}')",))

    class _Write:
        def __reduce__(self) -> tuple:
            return (open, (ctx.out, "w"))

    def lit(obj: object) -> str:  # a bytes literal, safe inside a str.format template
        return repr(pickle.dumps(obj)).replace("{", "{{").replace("}", "}}")

    out: list[Attack] = []
    for name, obj in (("os.system", _Sys()), ("check_call", _Spawn()), ("exec", _Exec())):
        blob = lit(obj)
        out += [
            (
                "pickle",
                f"pickle.loads -> {name}",
                f'MOD("pickle").loads({blob})',
            ),
            (
                "pickle",
                f"pd.read_pickle -> {name}",
                f'pd.read_pickle(MOD("io").BytesIO({blob}))',
            ),
        ]
    blob = lit(_Write())
    out.append(("pickle", "pickle.loads -> open(out, 'w')", f'MOD("pickle").loads({blob})'))
    return out


def _syscalls() -> list[Attack]:
    """Raw syscalls (kernel mode only). Reaching the kernel = not EPERM/ENOSYS from seccomp.

    Arguments are chosen so that a syscall that *did* reach the kernel does nothing: invalid
    magic numbers, NULL pointers, zero lengths, signal 0, or the harmless victim process.
    """
    calls = {
        "reboot (invalid magic)": ("reboot", "0, 0, 0, 0"),
        "init_module(NULL)": ("init_module", "0, 0, 0"),
        "finit_module(-1)": ("finit_module", "-1, 0, 0"),
        "delete_module(NULL)": ("delete_module", "0, 0"),
        "kexec_load(invalid flags)": ("kexec_load", "0, 0, 0, -1"),
        "sethostname(len too big)": ("sethostname", "0, 100000"),
        "settimeofday(NULL, NULL)": ("settimeofday", "0, 0"),
        "clock_settime(NULL)": ("clock_settime", "0, 0"),
        "mount(NULL)": ("mount", "0, 0, 0, 0, 0"),
        "umount2(NULL)": ("umount2", "0, 0"),
        "chroot(NULL)": ("chroot", "0"),
        "pivot_root(NULL)": ("pivot_root", "0, 0"),
        "unshare(0)": ("unshare", "0"),
        "setns(-1)": ("setns", "-1, 0"),
        "syslog(SIZE_BUFFER)": ("syslog", "10, 0, 0"),
        "perf_event_open(NULL)": ("perf_event_open", "0, 0, -1, -1, 0"),
        "keyctl(GET_KEYRING_ID)": ("keyctl", "0, 0, 0, 0, 0"),
        "bpf(-1)": ("bpf", "-1, 0, 0"),
        "io_uring_setup(0)": ("io_uring_setup", "0, 0"),
        "userfaultfd": ("userfaultfd", "0"),
        "open_by_handle_at(NULL)": ("open_by_handle_at", "-1, 0, 0"),
        "mknodat(NULL)": ("mknodat", "-100, 0, 0, 0"),
        "socket(AF_INET)": ("socket", "2, 1, 0"),
        "socket(AF_UNIX)": ("socket", "1, 1, 0"),
        "socket(AF_NETLINK)": ("socket", "16, 3, 0"),
        "ptrace(PEEKUSER victim)": ("ptrace", "3, {ctx.victim}, 0, 0"),
        "process_vm_readv(victim, 0 iov)": ("process_vm_readv", "{ctx.victim}, 0, 0, 0, 0, 0"),
        "pidfd_open(victim)": ("pidfd_open", "{ctx.victim}, 0"),
        "kcmp(victim)": ("kcmp", "{ctx.victim}, {ctx.victim}, 0, 0, 0"),
        "kill(victim, 0)": ("kill", "{ctx.victim}, 0"),
        "tgkill(victim, victim, 0)": ("tgkill", "{ctx.victim}, {ctx.victim}, 0"),
        "tkill(victim, 0)": ("tkill", "{ctx.victim}, 0"),
        "setpriority(victim, 19)": ("setpriority", "0, {ctx.victim}, 19"),
        "sched_setaffinity(victim, NULL)": ("sched_setaffinity", "{ctx.victim}, 0, 0"),
        "prlimit64(parent, get)": ("prlimit64", "_o.getppid(), 7, 0, 0"),
        "setrlimit(NULL)": ("setrlimit", "9, 0"),
        "execve(NULL)": ("execve", "0, 0, 0"),
        "fork": ("fork", ""),
        "clone(no CLONE_THREAD)": ("clone", "17, 0, 0, 0, 0"),
        "clone3": ("clone3", "0, 0"),
        "iopl(0)": ("iopl", "0"),
        "acct(bad pointer)": ("acct", "1"),
        "swapoff(NULL)": ("swapoff", "0"),
        "setxattrat(-1) (newer than the filter)": ("setxattrat", "-1, 0, 0, 0, 0, 0"),
    }
    out: list[Attack] = []
    for label, (name, args) in calls.items():
        if name not in NR:
            continue
        nr = NR[name]
        body = (
            "_o = os\n"
            f"_r, _e = sc({nr}, {args})\n"
            "if _e not in (1, 38):  # EPERM, ENOSYS: refused by seccomp\n"
            f"    print('{{ctx.token}}', _r, _e)\n"
            "if _r == 0 and " + ("True" if name in ("fork", "clone") else "False") + ":\n"
            "    os._exit(0)"
        )
        out.append(("syscall", label, body))
    return out


def attacks_for(ctx: Ctx) -> list[Attack]:
    return [
        *EXEC,
        *FORK,
        *READ,
        *PROC_READ,
        *WRITE,
        *CANARY,
        *METADATA,
        *NET,
        *SIGNALS,
        *RLIMIT,
        *EXHAUST,
        *_pickle_payloads(ctx),
        *_syscalls(),
    ]


# --- routes to a module, and module-name encodings ------------------------------------------

ENCODINGS: dict[str, Callable[[str], str]] = {
    "literal": repr,
    "chr list": lambda n: f"''.join(map(chr, {[ord(c) for c in n]!r}))",
    "hex": lambda n: f"bytes.fromhex({n.encode().hex()!r}).decode()",
    "reversed": lambda n: f"{n[::-1]!r}[::-1]",
    "rot13": lambda n: (
        f"{codecs.encode(n, 'rot13')!r}.translate(str.maketrans("
        "'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ', "
        "'nopqrstuvwxyzabcdefghijklmNOPQRSTUVWXYZABCDEFGHIJKLM'))"
    ),
    "base64 via codecs": lambda n: (
        "pd.io.common.os.sys.modules['codecs'].decode("
        f"{base64.b64encode(n.encode())!r}, 'base64').decode()"
    ),
    "json \\u escapes": lambda n: (
        "pd.io.common.os.sys.modules['json'].loads("
        + repr('"' + "".join(f"\\u{ord(c):04x}" for c in n) + '"')
        + ")"
    ),
    "concatenation": lambda n: " + ".join(repr(c) for c in n),
}

# Each route: (name, template). ``{n}`` is the encoded-name expression.
# ``fromlist=['__name__']`` makes ``__import__('a.b')`` return ``a.b``, not ``a``.
_FL = "None, None, ['__name__']"
ROUTES: dict[str, str] = {
    "__import__": f"__import__({{n}}, {_FL})",
    "__builtins__ dict": f"__builtins__['__import__']({{n}}, {_FL})",
    "real __import__ from pd's builtins": f"vars(pd)['__builtins__']['__import__']({{n}}, {_FL})",
    "np.__dict__ builtins": f"np.__dict__['__builtins__']['__import__']({{n}}, {_FL})",
    "sys.modules via pd.io.common": (
        "(pd.io.common.os.sys.modules.get({n}) or "
        f"vars(pd)['__builtins__']['__import__']({{n}}, {_FL}))"
    ),
    "exec(compile(..., 'lib.py'), {})": "_xmod({n})",
    "object.__subclasses__": "_submod({n})",
}
ROUTE_HELPERS = {
    "exec(compile(..., 'lib.py'), {})": (
        "def _xmod(n):\n    g = {}\n"
        "    exec(compile('import ' + n + ' as _x', '/usr/lib/python3/lib.py', 'exec'), g)\n"
        "    return g['_x']\n"
    ),
    "object.__subclasses__": (
        "def _submod(n):\n"
        "    for c in ().__class__.__base__.__subclasses__():\n"
        "        if c.__name__ == '_wrap_close':\n"
        "            s = c.__init__.__globals__['sys']\n"
        "            return s.modules.get(n) or c.__init__.__globals__['__builtins__']"
        "['__import__'](n, None, None, ['__name__'])\n"
        "    raise LookupError(n)\n"
    ),
}
# In kernel mode the bootstrap's guarded __import__ is still in the namespace; use routes that
# reach real modules, so that the kernel layer is what gets tested.
KERNEL_ROUTES = (
    "real __import__ from pd's builtins",
    "np.__dict__ builtins",
    "sys.modules via pd.io.common",
    "object.__subclasses__",
)

# --- path tricks for file targets ------------------------------------------------------------


def path_tricks(target: str) -> dict[str, tuple[str, str]]:
    """name -> (setup code, path expression)."""
    rel = "../" * 30 + target.lstrip("/")
    return {
        "absolute": ("", repr(target)),
        "/proc/self/root": ("", repr("/proc/self/root" + target)),
        "../ run": ("", repr(rel)),
        "doubled slashes": ("", repr(target.replace("/", "//"))),
        "/./ segments": ("", repr(target.replace("/", "/./"))),
        "bytes": ("", repr(target.encode())),
        "pathlib": ("", f'MOD("pathlib").Path({target!r})'),
        "symlink in cwd": (
            f"MOD(\"os\").symlink({target!r}, 'lnk')\n",
            "'lnk'",
        ),
        "hard link in cwd": (f"MOD(\"os\").link({target!r}, 'hl')\n", "'hl'"),
        "__fspath__ TOCTOU": (
            "class _P:\n    n = 0\n    def __fspath__(self):\n        _P.n += 1\n"
            f"        return {target!r} if _P.n % 2 else 'ok.txt'\n",
            "_P()",
        ),
        "__fspath__ TOCTOU (even)": (
            "class _P:\n    n = 0\n    def __fspath__(self):\n        _P.n += 1\n"
            f"        return 'ok.txt' if _P.n % 2 else {target!r}\n",
            "_P()",
        ),
        "str subclass": (
            "class _S(str):\n    def startswith(self, *a):\n        return True\n"
            "    def __fspath__(self):\n        return str(self)\n",
            f"_S({target!r})",
        ),
    }


# --- preludes and wrappers ---------------------------------------------------------------------

PRELUDES: dict[str, str] = {
    "none": "",
    "reset audit-hook state from a traceback": (
        "try:\n    open('/nonexistent/x')\nexcept BaseException as _e:\n"
        "    _tb = _e.__traceback__\n    while _tb:\n"
        "        for _v in list(_tb.tb_frame.f_locals.values()):\n"
        "            if isinstance(_v, dict):\n"
        "                for _k in list(_v):\n"
        "                    if isinstance(_v[_k], bool):\n"
        "                        _v[_k] = False\n"
        "        _tb = _tb.tb_next\n"
    ),
    "monkeypatch posixpath._joinrealpath": (
        "_pp = pd.io.common.os.path\n_cwd = pd.io.common.os.getcwd()\n"
        "_pp._joinrealpath = lambda path, rest, strict, seen: (_cwd, True)\n"
    ),
    "isinstance lies about int": (
        "_bi = vars(pd)['__builtins__']\n_ri = _bi['isinstance']\n"
        "_bi['isinstance'] = lambda o, t: True if t is int else _ri(o, t)\n"
    ),
}

WRAPPERS: dict[str, Callable[[str], str]] = {
    "plain": lambda b: b,
    "function": lambda b: "def _attack():\n" + textwrap.indent(b, "    ") + "\n_attack()\n",
    "try/except swallow": lambda b: (
        "try:\n" + textwrap.indent(b, "    ") + "\nexcept BaseException as _e:\n"
        "    print('swallowed', type(_e).__name__)\n"
    ),
    "exec(str)": lambda b: f"exec({b!r})\n",
    "exec with fresh globals": lambda b: (
        f"exec(compile({b!r}, '/usr/lib/python3/lib.py', 'exec'), "
        "{'pd': pd, 'np': np, 'plt': plt, '_xmod': globals().get('_xmod'), "
        "'_submod': globals().get('_submod'), 'sc': globals().get('sc'), "
        "'os': globals().get('os')})\n"
    ),
}


@dataclass
class Case:
    id: int
    category: str
    attack: str
    mode: str
    route: str
    encoding: str
    path_trick: str
    prelude: str
    wrapper: str
    code: str = field(repr=False)


def _render_mods(body: str, route: str, encoding: str) -> str:
    out, i = [], 0
    while (j := body.find('MOD("', i)) != -1:
        k = body.index('")', j)
        name = body[j + 5 : k]
        out.append(body[i:j])
        out.append(ROUTES[route].format(n=ENCODINGS[encoding](name)))
        i = k + 2
    out.append(body[i:])
    return "".join(out)


def generate(n: int, seed: int, ctx_for: Callable[[int], Ctx]) -> list[Case]:
    """``n`` snippets. Every attack appears at least once (round-robin), the rest is random."""
    rng = random.Random(seed)
    names = [a[1] for a in attacks_for(ctx_for(0))]
    order = list(range(len(names)))
    rng.shuffle(order)
    cases: list[Case] = []
    for i in range(n):
        ctx = ctx_for(i)
        attacks = attacks_for(ctx)
        pick = order[i % len(order)] if i < 2 * len(order) else rng.randrange(len(attacks))
        category, aname, body = attacks[pick]
        mode = "kernel" if category == "syscall" else rng.choice(["full", "full", "kernel"])
        route = rng.choice(KERNEL_ROUTES if mode == "kernel" else list(ROUTES))
        encoding = rng.choice(list(ENCODINGS))
        prelude = "none" if mode == "kernel" else rng.choice(list(PRELUDES))
        wrapper = rng.choice(list(WRAPPERS))
        trick = "-"
        setup = ""
        if "{PATH}" in body:
            target = (
                ctx.secret
                if category == "file_read"
                else (ctx.poison if rng.random() < 0.2 else ctx.out)
            )
            tricks = path_tricks(target)
            trick = rng.choice(list(tricks))
            setup, expr = tricks[trick]
            body = body.replace("{PATH}", expr)
        body = (setup + body).replace("{PATH}", "")
        body = body.format(ctx=ctx)
        body = _render_mods(body, route, encoding)
        head = ROUTE_HELPERS.get(route, "")
        if mode == "kernel":
            head = _layers.CTYPES_PRELUDE + head
        code = head + PRELUDES[prelude] + WRAPPERS[wrapper](body)
        cases.append(Case(i, category, aname, mode, route, encoding, trick, prelude, wrapper, code))
    return cases


# --------------------------------------------------------------------------- the oracles


class _Listener:
    """Counts TCP and UNIX-socket connections (and UDP datagrams) from the snippets."""

    def __init__(self, unix_path: str) -> None:
        self.hits: list[str] = []
        self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp.bind(("127.0.0.1", 0))
        self.tcp.listen(64)
        self.port = self.tcp.getsockname()[1]
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", self.port))
        self.unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.unix.bind(unix_path)
        self.unix.listen(64)
        self._stop = False
        for s in (self.tcp, self.udp, self.unix):
            s.settimeout(0.2)
            threading.Thread(target=self._serve, args=(s,), daemon=True).start()

    def _serve(self, s: socket.socket) -> None:
        while not self._stop:
            try:
                if s.type == socket.SOCK_DGRAM:
                    s.recvfrom(64)
                    self.hits.append("udp")
                else:
                    conn, _ = s.accept()
                    self.hits.append("unix" if s.family == socket.AF_UNIX else "tcp")
                    conn.close()
            except (TimeoutError, OSError):
                continue

    def close(self) -> None:
        self._stop = True
        for s in (self.tcp, self.udp, self.unix):
            s.close()


@dataclass
class Env:
    root: Path
    secret_token: str
    listener: _Listener
    victim: subprocess.Popen
    mpl_dir: Path

    def ctx(self, i: int) -> Ctx:
        return Ctx(
            case_id=i,
            secret=str(self.root / "secret.txt"),
            out=str(self.root / "out" / f"out_{i}"),
            canary=str(self.root / "canary" / str(i) / "canary.txt"),
            marker=str(self.root / "markers" / f"pwned_{i}"),
            poison=str(self.mpl_dir / f"poison_{i}"),
            readable=str(self.mpl_dir / f"fuzz_readable_{i}.txt"),
            port=self.listener.port,
            unix=str(self.root / "listener.sock"),
            victim=self.victim.pid,
        )


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Env]:
    root = tmp_path_factory.mktemp("fuzz")
    for d in ("out", "canary", "markers"):
        (root / d).mkdir()
    token = f"{SECRET_PREFIX}-{os.urandom(8).hex()}"
    (root / "secret.txt").write_text(f"{token}\n")
    mpl = python_runner._mpl_cache_dir()
    assert mpl, "matplotlib cache dir unavailable"
    listener = _Listener(str(root / "listener.sock"))
    victim = subprocess.Popen(["sleep", "3600"])
    try:
        yield Env(root, token, listener, victim, Path(mpl))
    finally:
        victim.kill()
        victim.wait()
        for p in Path(mpl).glob("fuzz_readable_*"):
            p.unlink()
        listener.close()


@dataclass
class Outcome:
    case: Case
    ok: bool
    error: str | None
    duration_s: float
    stdout: str
    problems: list[str]


def _check(case: Case, res: object, duration: float, env: Env) -> list[str]:
    ctx = env.ctx(case.id)
    problems = []
    stdout = getattr(res, "stdout", "")
    stderr = getattr(res, "stderr", "")
    if ctx.token in stdout:
        problems.append("attack reported success inside the sandbox")
    if env.secret_token in stdout or env.secret_token in stderr:
        problems.append("secret file contents leaked")
    if Path(ctx.marker).exists():
        problems.append("a spawned process ran (marker file exists)")
    if Path(ctx.out).exists():
        problems.append("file created outside the sandbox")
    if Path(ctx.poison).exists():
        problems.append("shared matplotlib cache written")
    readable = Path(ctx.readable)
    if not readable.exists() or readable.read_text() != "readable\n":
        problems.append("readable file deleted or modified")
    else:
        st = readable.stat()
        if stat.S_IMODE(st.st_mode) != 0o644 or st.st_uid != os.getuid() or st.st_mtime < 1e6:
            problems.append("readable file's metadata changed")
        if hasattr(os, "listxattr") and os.listxattr(readable):
            problems.append("readable file's xattrs changed")
    canary = Path(ctx.canary)
    if not canary.exists():
        problems.append("canary deleted or moved")
    else:
        st = canary.stat()
        if canary.read_text() != "canary\n" or stat.S_IMODE(st.st_mode) != 0o600:
            problems.append("canary modified")
        if st.st_mtime < 1_000_000:
            problems.append("canary timestamps changed")
    if duration > TIMEOUT_S + SLACK_S:
        problems.append(f"not killed within limits ({duration:.1f}s)")
    return problems


def _run_case(case: Case, env: Env) -> Outcome:
    ctx = env.ctx(case.id)
    readable = Path(ctx.readable)
    readable.write_text("readable\n")
    readable.chmod(0o644)
    canary = Path(ctx.canary)
    canary.parent.mkdir(exist_ok=True)
    canary.write_text("canary\n")
    canary.chmod(0o600)
    t0 = time.monotonic()
    with _layers.kernel_only(case.mode == "kernel"):
        res = python_runner.run_python(
            case.code,
            timeout_s=TIMEOUT_S,
            memory_limit_mb=MEMORY_MB,
            artifacts_dir=env.root / "artifacts" / str(case.id),
        )
    duration = time.monotonic() - t0
    problems = _check(case, res, duration, env)
    return Outcome(case, res.ok, res.error, duration, res.stdout[:500], problems)


# --------------------------------------------------------------------------- the tests


def _ctx_stub(i: int) -> Ctx:
    return Ctx(i, "/x/secret", "/x/out", "/x/canary", "/x/m", "/x/p", "/x/r", 1, "/x/s", 2)


def test_fuzz_corpus_is_valid_and_covers_every_attack() -> None:
    """Cheap (no subprocesses, not slow): the generator yields compilable code covering
    every attack, so a grammar mistake fails in the normal test run, not only at night."""
    cases = generate(N, SEED, _ctx_stub)
    assert len(cases) == N
    for c in cases:
        compile(c.code, f"<case {c.id}>", "exec")
    assert {c.attack for c in cases} == {a[1] for a in attacks_for(_ctx_stub(0))}
    assert {c.category for c in cases} >= {
        "exec",
        "fork_bomb",
        "file_read",
        "file_write",
        "canary",
        "network",
        "signal",
        "rlimit",
        "memory",
        "metadata",
        "pickle",
        "proc",
        "syscall",
    }
    assert {c.mode for c in cases} == {"full", "kernel"}
    assert len({c.code for c in cases}) > 0.9 * N


@pytest.mark.slow
@pytest.mark.skipif(not sys.platform.startswith("linux") or not NR, reason="Linux x86_64/arm64")
def test_fuzz_escape_attempts_are_blocked_or_killed(
    env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _layers.install(monkeypatch, tmp_path / "shim")
    cases = generate(N, SEED, env.ctx)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        outcomes = list(pool.map(lambda c: _run_case(c, env), cases))

    if env.victim.poll() is not None:
        for o in outcomes:
            if o.case.category in ("signal", "syscall"):
                o.problems.append("victim process may have been killed by this case")
    stray = env.listener.hits

    if path := os.environ.get("SANDBOX_FUZZ_REPORT"):
        Path(path).write_text(
            json.dumps(
                {
                    "seed": SEED,
                    "n": N,
                    "network_hits": stray,
                    "outcomes": [{**asdict(o), "case": {**asdict(o.case)}} for o in outcomes],
                },
                indent=1,
            )
        )

    escaped = [o for o in outcomes if o.problems]
    lines = [
        f"#{o.case.id} [{o.case.mode}] {o.case.category}/{o.case.attack} "
        f"route={o.case.route} enc={o.case.encoding} path={o.case.path_trick} "
        f"prelude={o.case.prelude} wrap={o.case.wrapper}: {'; '.join(o.problems)} "
        f"(ok={o.ok}, error={o.error!r}, stdout={o.stdout[:120]!r})"
        for o in escaped
    ]
    assert not stray, f"connections reached the host listeners: {stray}"
    assert not escaped, (
        f"{len(escaped)}/{len(outcomes)} snippets got through (seed {SEED}):\n"
        + "\n".join(lines)
        + "\n\nFirst offending snippet:\n"
        + escaped[0].case.code
    )
