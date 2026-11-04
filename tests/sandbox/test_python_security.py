"""Security tests for the Python runner: each attack must be blocked and reported as a
structured error (never raised in the parent), and must have no effect outside the sandbox."""

from __future__ import annotations

import os
import socket
import threading
from pathlib import Path

import pytest

from analyst_agent.sandbox._errors import kind_of
from analyst_agent.sandbox.python_runner import run_python

_BYPASS_PREFIX = "_imp = vars(pd)['__builtins__']['__import__']\n"


def _bypass_import_guard(code: str) -> str:
    """Rewrite ``import a, b.c`` lines to fetch modules through the *real* ``__import__``.

    This simulates an attacker who has escaped the import allowlist (it is a Python-level
    guard and can be bypassed) so that the deeper layers - audit hook, socket patch, OS
    limits - are tested on their own.
    """
    out = []
    for line in code.splitlines():
        if line.startswith("import ") and " as " not in line:
            mods = [m.strip() for m in line[len("import ") :].split(",")]
            out += [f"{m.split('.')[0]} = _imp({m!r})" for m in mods]
        else:
            out.append(line)
    return _BYPASS_PREFIX + "\n".join(out)


@pytest.fixture(params=["direct", "bypass_import_guard"])
def prep(request):
    return (lambda c: c) if request.param == "direct" else _bypass_import_guard


def _blocked(res, *types: str) -> None:
    assert not res.ok, f"expected failure, got stdout={res.stdout!r}"
    assert res.error is not None
    if types:
        assert kind_of(res.error) in types, res.error


# --- infinite loops / CPU ------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "while True:\n    pass",
        "import itertools\nfor _ in itertools.count():\n    pass",
        "def f():\n    while True:\n        try:\n            pass\n"
        "        except BaseException:\n            pass\nf()",
        # ignoring signals doesn't help: the parent SIGKILLs the whole process group
        _bypass_import_guard(
            "import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True: pass"
        ),
    ],
)
def test_infinite_loop_is_killed(code):
    res = run_python(code, timeout_s=2)
    _blocked(res, "Timeout")
    assert res.duration_ms < 10_000


def test_output_flood_is_truncated():
    res = run_python("for _ in range(10**6):\n    print('x' * 100)", timeout_s=20)
    assert len(res.stdout) < 100_000
    assert "truncated" in res.stdout


# --- memory bombs --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "x = bytearray(8 * 1024**3)",
        "x = 'a' * (8 * 1024**3)",
        "x = [0] * (2 * 1024**3)",
        "import numpy as np\nx = np.ones((60000, 60000))",
        "chunks = []\nwhile True:\n    chunks.append(bytearray(50 * 1024**2))",
    ],
)
def test_memory_bomb_is_contained(code):
    res = run_python(code, timeout_s=30, memory_limit_mb=512)
    _blocked(res, "MemoryLimitExceeded")


# --- filesystem ----------------------------------------------------------------------------


@pytest.fixture()
def outside_dir(tmp_path: Path) -> Path:
    d = tmp_path / "outside"
    d.mkdir()
    return d


@pytest.mark.parametrize(
    "template",
    [
        "open({p!r}, 'w').write('pwned')",
        "open({p!r}, 'a').write('pwned')",
        "import pandas as pd\npd.DataFrame({{'a': [1]}}).to_csv({p!r})",
        "import numpy as np\nnp.save({p!r}, np.zeros(3))",
        "plt.plot([1]); plt.savefig({png!r})",
        "import os\nfd = os.open({p!r}, os.O_WRONLY | os.O_CREAT)\nos.write(fd, b'pwned')",
        "import io\nio.FileIO({p!r}, 'w').write(b'pwned')",
        "open('../../' + {rel!r}, 'w').write('pwned')",
        "import os\nos.symlink({p!r}, 'link')\nopen('link', 'w').write('pwned')",
        "import pathlib\npathlib.Path({p!r}).write_text('pwned')",
        "import os\nos.rename('__sandbox__/code.py', {p!r})",
        "import os\nos.mkdir({d!r} + '/newdir')",
    ],
)
def test_writes_outside_temp_dir_blocked(outside_dir, template, prep):
    target = outside_dir / "pwned.txt"
    code = template.format(
        p=str(target),
        rel=str(target).lstrip("/"),
        d=str(outside_dir),
        png=str(outside_dir / "pwned.png"),
    )
    res = run_python(prep(code), timeout_s=30)
    _blocked(res, "PermissionDenied", "FileNotFoundError", "OSError")
    assert list(outside_dir.iterdir()) == []


def test_delete_outside_temp_dir_blocked(outside_dir, prep):
    victim = outside_dir / "keep.txt"
    victim.write_text("important")
    for code in (
        f"import os\nos.remove({str(victim)!r})",
        f"import os\nos.unlink({str(victim)!r})",
        f"import shutil\nshutil.rmtree({str(outside_dir)!r})",
        f"import pathlib\npathlib.Path({str(victim)!r}).unlink()",
        f"import os\nos.chmod({str(victim)!r}, 0o777)",
    ):
        res = run_python(prep(code), timeout_s=30)
        _blocked(res, "PermissionDenied")
    assert victim.read_text() == "important"


@pytest.mark.parametrize(
    "code",
    [
        "print(open('/etc/passwd').read())",
        "import pandas as pd\nprint(pd.read_csv('/etc/passwd', sep=':', header=None))",
        "import os\nprint(os.listdir('/root'))",
        "import os\nprint(os.listdir(os.path.expanduser('~') + '/..'))",
        "import glob\nprint(glob.glob('/home/*/*'))",
        "print(open('/proc/1/environ', 'rb').read())",
    ],
)
def test_reads_outside_allowlist_blocked(code, prep):
    res = run_python(prep(code), timeout_s=30)
    _blocked(res, "PermissionDenied")
    assert "root:" not in res.stdout


def test_cannot_read_parent_project_files():
    secret = Path(__file__).resolve()
    res = run_python(f"print(open({str(secret)!r}).read())", timeout_s=30)
    _blocked(res, "PermissionDenied")


def test_writes_inside_temp_dir_allowed_and_cleaned_up():
    res = run_python(
        _bypass_import_guard(
            "import os\nopen('scratch.txt', 'w').write('ok')\n"
            "os.makedirs('sub', exist_ok=True)\nprint(os.getcwd())"
        ),
        timeout_s=30,
    )
    assert res.ok, res.error
    workdir = res.stdout.strip()
    assert workdir and not os.path.exists(workdir), "temp dir must be removed after the run"


# --- network -------------------------------------------------------------------------------


@pytest.fixture()
def local_listener():
    """A real TCP server in the *parent*: proves the child can't reach even localhost."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    srv.settimeout(10)
    hits: list[bytes] = []

    def _accept():
        try:
            conn, _ = srv.accept()
            hits.append(conn.recv(100))
            conn.close()
        except OSError:
            pass

    t = threading.Thread(target=_accept, daemon=True)
    t.start()
    yield srv.getsockname()[1], hits
    srv.close()


@pytest.mark.parametrize(
    "template",
    [
        "import socket\ns = socket.socket()\ns.connect(('127.0.0.1', {port}))\ns.send(b'leak')",
        "import socket\nsocket.create_connection(('127.0.0.1', {port})).send(b'leak')",
        "import _socket\ns = _socket.socket()\ns.connect(('127.0.0.1', {port}))",
        "import socket\nprint(socket.getaddrinfo('example.com', 80))",
        "import urllib.request\nurllib.request.urlopen('http://127.0.0.1:{port}/')",
        "import http.client\nc = http.client.HTTPConnection('127.0.0.1', {port})\n"
        "c.request('GET', '/')",
        "import importlib\nsock = importlib.import_module('socket')\n"
        "sock.socket().connect(('127.0.0.1', {port}))",
        "import pandas as pd\npd.read_csv('http://127.0.0.1:{port}/x.csv')",
    ],
)
def test_network_blocked(local_listener, template, prep):
    port, hits = local_listener
    res = run_python(prep(template.format(port=port)), timeout_s=30)
    _blocked(res)
    assert hits == []


# --- process / escape attempts ---------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "import os\nos.system('touch /tmp/pwned_by_sandbox')",
        "import subprocess\nsubprocess.run(['id'])",
        "import os\nos.execv('/bin/sh', ['sh', '-c', 'id'])",
        "import os\nos.fork()",
        "import os\nos.kill(os.getppid(), 9)",
        "import os, signal\nos.kill(1, signal.SIGTERM)",
        "import ctypes\nctypes.CDLL(None).system(b'id')",
        "import resource\nresource.setrlimit(resource.RLIMIT_AS, (-1, -1))",
        "import gc\nprint(len(gc.get_objects()))",
        "import multiprocessing\nmultiprocessing.Process(target=print).start()",
        "import pty\npty.spawn('/bin/sh')",
    ],
)
def test_process_and_escape_attempts_blocked(code, prep):
    res = run_python(prep(code), timeout_s=30)
    _blocked(res)
    assert not os.path.exists("/tmp/pwned_by_sandbox")


def test_disallowed_imports_from_user_code():
    # includes modules already in sys.modules (loaded by pandas/numpy) - those don't fire the
    # audit "import" event and are caught by the guarded __import__ instead
    for mod in (
        "requests",
        "sqlite3",
        "shutil_but_fake",
        "pickle",
        "ctypes",
        "asyncio",
        "os",
        "subprocess",
        "shutil",
        "socket",
        "importlib",
        "builtins",
        "sys",
    ):
        res = run_python(f"import {mod}", timeout_s=30)
        _blocked(res, "PermissionDenied", "ModuleNotFoundError")


def test_environment_is_scrubbed(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid")
    res = run_python(_bypass_import_guard("import os\nprint(sorted(os.environ))"), timeout_s=30)
    assert res.ok, res.error
    assert "PATH" in res.stdout
    assert "ANTHROPIC_API_KEY" not in res.stdout and "sk-should-not-leak" not in res.stdout
    assert "HTTPS_PROXY" not in res.stdout


def test_parent_never_raises_on_garbage():
    for code in ("def (:", "raise SystemExit(3)", "import sys\nsys.exit(0)", "\x00"):
        res = run_python(code, timeout_s=30)
        assert res.error is None or kind_of(res.error)


# --- native-code escapes (review findings H1/H2): only the kernel layer stops these ----------

_FORK_EXEC = """
o = pd.io.common.os
m = o.sys.modules['_posixsubprocess']
r, w = o.pipe()
er, ew = o.pipe()
args = [[b'/bin/sh', b'-c', b'touch {marker}'], [b'/bin/sh'], True, (), None, None,
        -1, -1, -1, w, -1, w, er, ew, False, False, -1, None, None, None, -1, None]
try:
    m.fork_exec(*args, False)  # 3.11.x with process_group (23 args)
except TypeError:
    m.fork_exec(*args)
print('spawned')
"""


def test_fork_exec_via_module_attribute_is_blocked(tmp_path):
    """``_posixsubprocess.fork_exec`` is reachable without an import and raises no audit event;
    before the seccomp filter it spawned a shell with network and full file access."""
    marker = tmp_path / "pwned"
    res = run_python(_FORK_EXEC.format(marker=marker), timeout_s=30)
    _blocked(res)
    assert "Operation not permitted" in (res.error or "") or "PermissionDenied" in res.error
    assert not marker.exists()


def test_native_file_read_outside_roots_is_blocked(tmp_path):
    """pyarrow's C++ I/O bypasses the audit hook's ``open`` event; Landlock stops it."""
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET")
    code = (
        "pa = pd.io.common.os.sys.modules['pyarrow']\n"
        f"print(pa.input_stream({str(secret)!r}).read())"
    )
    res = run_python(code, timeout_s=30)
    _blocked(res)
    assert "TOPSECRET" not in res.stdout


def test_native_file_write_outside_work_dir_is_blocked(tmp_path):
    target = tmp_path / "escaped.parquet"
    res = run_python(f"pd.DataFrame({{'a': [1]}}).to_parquet({str(target)!r})", timeout_s=30)
    _blocked(res)
    assert not target.exists()


def test_native_io_inside_work_dir_still_works():
    res = run_python(
        "pd.DataFrame({'a': [1, 2]}).to_parquet('t.parquet')\n"
        "print(pd.read_parquet('t.parquet')['a'].sum())\n"
        "from scipy import stats\nprint(round(stats.norm.cdf(0), 2))",
        timeout_s=60,
    )
    assert res.ok, res.error
    assert res.stdout.split() == ["3", "0.5"]


def test_rlimits_applied_without_preexec_fn(monkeypatch):
    """Limits are set by the child itself: ``preexec_fn`` is unsafe in a threaded server."""
    import subprocess

    from analyst_agent.sandbox import python_runner

    seen: dict = {}
    real_popen = subprocess.Popen

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(python_runner.subprocess, "Popen", spy)
    res = run_python("print(open('/proc/self/limits').read())", timeout_s=30, memory_limit_mb=512)
    assert res.ok, res.error
    assert seen.get("preexec_fn") is None
    lines = {ln.split("  ")[0]: ln for ln in res.stdout.splitlines()}
    assert str(512 * 1024 * 1024) in lines["Max address space"]
    assert "0" in lines["Max core file size"].split()


def test_seccomp_filter_program_is_well_formed():
    from analyst_agent.sandbox._child import _oslock

    for machine in ("x86_64", "aarch64"):
        prog = _oslock.build_filter(machine)
        assert len(prog) % 8 == 0 and 0 < len(prog) // 8 < 256


# --- gaps found by the escape fuzzer (tests/sandbox/test_fuzz_escape.py) ---------------------

_TAMPER = """
try:
    open('/nonexistent/x')
except PermissionError as e:
    tb = e.__traceback__
    while tb:
        for v in list(tb.tb_frame.f_locals.values()):
            if isinstance(v, dict):
                for k in list(v):
                    if isinstance(v[k], bool):
                        v[k] = not v[k]
        tb = tb.tb_next
"""


@pytest.mark.parametrize(
    "attack",
    [
        "g = {}\nexec(compile('import ctypes', 'lib.py', 'exec'), g)\ng['ctypes'].CDLL(None)",
        "pd.io.common.os.sys.modules['gc'].get_objects()",
        "pd.io.common.os.sys.modules['ctypes'].CDLL(None)",
    ],
)
def test_user_phase_checks_survive_traceback_tampering(attack):
    """The audit hook's user-phase flag used to sit in a dict that any SandboxViolation's
    traceback exposed (``tb_frame.f_locals``); flipping it switched off the ctypes, gc and
    import checks. The user-phase checks now live in a separate hook with no mutable state."""
    res = run_python(_TAMPER + attack, timeout_s=30)
    _blocked(res, "PermissionDenied")


@pytest.mark.parametrize(
    "call",
    [
        "o.setpriority(o.PRIO_PROCESS, {victim}, 19)",
        "o.setpriority(o.PRIO_PROCESS, 0, -20)",
        "o.sched_setaffinity({victim}, {{0}})",
        "o.sched_setscheduler(0, o.SCHED_FIFO, o.sched_param(1))",
    ],
)
def test_scheduling_changes_are_blocked(call):
    """No audit event covers these, so in production they worked: renicing other processes of
    the service user, and SCHED_FIFO for the sandbox itself (a busy loop then starves a core)."""
    victim = __import__("subprocess").Popen(["sleep", "60"])
    try:
        before = os.getpriority(os.PRIO_PROCESS, victim.pid)
        code = "o = pd.io.common.os\n" + call.format(victim=victim.pid) + "\nprint('changed')"
        res = run_python(code, timeout_s=30)
        _blocked(res)
        assert "changed" not in res.stdout
        assert os.getpriority(os.PRIO_PROCESS, victim.pid) == before
    finally:
        victim.kill()
        victim.wait()


@pytest.fixture()
def kernel_only(monkeypatch, tmp_path):
    """Run with the Python guards removed (see tests/sandbox/_layers.py)."""
    from . import _layers

    _layers.install(monkeypatch, tmp_path / "shim")

    def run(code: str, **kw):
        with _layers.kernel_only():
            return run_python(_layers.CTYPES_PRELUDE + code, timeout_s=30, **kw)

    return run


@pytest.mark.skipif(not os.path.exists("/proc/self/status"), reason="Linux only")
def test_kernel_layer_blocks_unix_socket_connect(kernel_only, tmp_path):
    """Landlock (ABI <= 7) does not mediate connect() to a pathname UNIX socket, and seccomp
    allowed AF_UNIX: e.g. /var/run/docker.sock was reachable once the audit hook was bypassed."""
    path = tmp_path / "s.sock"
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(str(path))
    srv.listen(1)
    srv.settimeout(0.5)
    try:
        res = kernel_only(
            "s = vars(pd)['__builtins__']['__import__']('socket')\n"
            "c = [k for k in s.socket.__mro__ if k.__module__ == '_socket'][0](s.AF_UNIX)\n"
            f"c.connect({str(path)!r})\nprint('connected')"
        )
        _blocked(res)
        assert "connected" not in res.stdout
        with pytest.raises(TimeoutError):
            srv.accept()
    finally:
        srv.close()


def _syscall_numbers() -> dict[str, int]:
    from analyst_agent.sandbox._child import _oslock

    return _oslock._ARCH[__import__("platform").machine()][1]


@pytest.mark.skipif(not os.path.exists("/proc/self/status"), reason="Linux only")
def test_kernel_layer_refuses_host_wide_and_cross_process_syscalls(kernel_only):
    """Syscalls Landlock does not cover. The service often runs as root in a container, where
    these reached the kernel: reboot, module loading, hostname and clock changes, mounts, the
    kernel log, other processes' scheduling. Arguments are harmless even if one got through."""
    nr = _syscall_numbers()
    victim = __import__("subprocess").Popen(["sleep", "60"])
    calls = {
        "reboot": "0, 0, 0, 0",
        "init_module": "0, 0, 0",
        "sethostname": "0, 100000",
        "settimeofday": "0, 0",
        "mount": "0, 0, 0, 0, 0",
        "syslog": "10, 0, 0",
        "unshare": "0",
        "setpriority": f"0, {victim.pid}, 19",
        "kill": f"{victim.pid}, 0",
        "tgkill": f"{victim.pid}, {victim.pid}, 0",
        "pidfd_open": f"{victim.pid}, 0",
        "fchmod": "-1, 0o644",
        "utimensat": "-1, 0, 0, 0",
        "fsetxattr": "-1, 0, 0, 0, 0",
        "socket": "1, 1, 0",
    }
    try:
        code = "".join(f"print({k!r}, *sc({nr[k]}, {a}))\n" for k, a in calls.items())
        code += "print('setxattrat', *sc(463, -1, 0, 0, 0, 0, 0))\n"  # newer than the filter
        code += "print('self', *sc(" + str(nr["kill"]) + ", os.getpid(), 0))\n"
        res = kernel_only(code)
        assert res.ok, res.error
        got = {ln.split()[0]: ln.split()[1:] for ln in res.stdout.splitlines()}
        for name in calls:
            assert got[name] == ["-1", "1"], (name, got[name])  # EPERM
        assert got["setxattrat"] == ["-1", "38"]  # ENOSYS
        assert got["self"] == ["0", "0"]  # signalling itself still works
        assert victim.poll() is None
    finally:
        victim.kill()
        victim.wait()


@pytest.mark.skipif(not os.path.exists("/proc/self/status"), reason="Linux only")
def test_kernel_layer_blocks_metadata_changes_on_readable_files(kernel_only):
    """Landlock grants reads under the read roots but does not mediate chmod/chown/utimes,
    xattrs or chattr ioctls, so a read-only fd was enough to change a file's metadata."""
    from analyst_agent.sandbox.python_runner import _mpl_cache_dir

    target = Path(_mpl_cache_dir()) / f"meta-test-{os.getpid()}.txt"
    target.write_text("x")
    target.chmod(0o644)
    before = target.stat()
    try:
        code = f"fd = os.open({str(target)!r}, os.O_RDONLY)\n" + "".join(
            f"try:\n    {stmt}\n    print('changed')\nexcept OSError:\n    pass\n"
            for stmt in (
                "os.fchmod(fd, 0o777)",
                f"os.chmod({str(target)!r}, 0o777)",
                "os.fchown(fd, 1, 1)",
                "os.utime(fd, (0, 0))",
                "os.setxattr(fd, 'user.x', b'1')",
                "vars(pd)['__builtins__']['__import__']('fcntl').ioctl(fd, 0x40086602, bytes(8))",
            )
        )
        res = kernel_only(code)
        assert res.ok, res.error
        assert "changed" not in res.stdout
        after = target.stat()
        assert (after.st_mode, after.st_uid, after.st_mtime) == (
            before.st_mode,
            before.st_uid,
            before.st_mtime,
        )
    finally:
        target.unlink()


def test_seccomp_signal_rule_embeds_the_child_pid():
    import struct

    from analyst_agent.sandbox._child import _oslock

    prog = _oslock.build_filter("x86_64", pid=4242)
    ks = [struct.unpack("=HBBI", prog[i : i + 8])[3] for i in range(0, len(prog), 8)]
    assert 4242 in ks
    assert _oslock.build_filter("x86_64", pid=1) != prog
