"""In-process unit tests for the kernel-confinement helpers.

``apply_landlock`` / ``apply_seccomp`` are irreversible, so they are only *applied* in the
sandbox child (see ``test_python_security.py``). Here the raw syscalls are replaced with
recorders so the rule-building logic and the fail-open / fail-closed branches are checked
without confining the test process.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

import pytest

from analyst_agent.sandbox._child import _oslock as L


class _Calls(list):
    abi: dict[str, int]
    closed: list[int]


@pytest.fixture()
def syscalls(monkeypatch) -> _Calls:
    calls = _Calls()
    abi = {"value": 6}

    def fake_syscall(nr: int, *args: Any) -> int:
        calls.append((nr, *args))
        if nr == L._SYS_LANDLOCK_CREATE_RULESET and args and args[0] is None:
            return abi["value"]  # version query
        if nr == L._SYS_LANDLOCK_CREATE_RULESET:
            return 999  # ruleset fd
        return 0

    closed: list[int] = []
    real_close = L.os.close

    def fake_close(fd: int) -> None:
        if fd == 999:
            closed.append(fd)
        else:
            real_close(fd)

    monkeypatch.setattr(L, "_syscall", fake_syscall)
    monkeypatch.setattr(L, "_no_new_privs", lambda: calls.append(("no_new_privs",)))
    monkeypatch.setattr(L.os, "close", fake_close)
    calls.abi, calls.closed = abi, closed
    return calls


def _rules(calls: list[tuple[Any, ...]]) -> list[int]:
    out = []
    for c in calls:
        if c[0] == L._SYS_LANDLOCK_ADD_RULE:
            access, _fd = struct.unpack("=Qi", c[3].raw)
            out.append(access)
    return out


def test_handled_fs_grows_with_abi() -> None:
    assert L._handled_fs(1) == (1 << 13) - 1
    assert L._handled_fs(2) & L._FS_REFER and not L._handled_fs(2) & L._FS_TRUNCATE
    assert L._handled_fs(3) & L._FS_TRUNCATE and not L._handled_fs(3) & L._FS_IOCTL_DEV
    assert L._handled_fs(5) & L._FS_IOCTL_DEV


def test_landlock_abi_reports_zero_on_error(monkeypatch) -> None:
    def fail(*a: Any) -> int:
        raise OSError(38, "ENOSYS")

    monkeypatch.setattr(L, "_syscall", fail)
    assert L.landlock_abi() == 0
    with pytest.raises(L.ConfinementError, match="not supported"):
        L.apply_landlock(["/"], [])


def test_apply_landlock_rules(syscalls, tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    a_file = tmp_path / "f.txt"
    a_file.write_text("x")
    abi = L.apply_landlock(
        [str(tmp_path), str(a_file), "/dev/null", str(tmp_path / "missing")],
        [str(work), str(tmp_path)],
    )
    assert abi == 6
    create = next(
        c for c in syscalls if c[0] == L._SYS_LANDLOCK_CREATE_RULESET and c[1] is not None
    )
    # handled_fs, handled_net, scoped (ABI >= 6)
    assert len(create[1].raw) == 3 * 8
    rules = _rules(syscalls)
    # tmp_path (read+write merged), f.txt (file-only), /dev/null (read+write file), work (write)
    assert len(rules) == 4
    by_order = dict(zip(["tmp", "file", "devnull", "work"], rules, strict=True))
    assert by_order["tmp"] & L._FS_MAKE_REG  # write root merged into the read root
    assert by_order["file"] == L._FS_READ_FILE  # READ_DIR stripped for a file
    assert by_order["devnull"] == L._FS_READ_FILE | L._FS_WRITE_FILE
    assert not any(r & L._FS_EXECUTE for r in rules)
    assert ("no_new_privs",) in syscalls
    assert syscalls[-1][0] == L._SYS_LANDLOCK_RESTRICT_SELF
    assert syscalls.closed == [999]


@pytest.mark.parametrize(("abi", "fields"), [(1, 1), (3, 1), (4, 2), (5, 2)])
def test_apply_landlock_attr_size_by_abi(syscalls, abi: int, fields: int) -> None:
    syscalls.abi["value"] = abi
    assert L.apply_landlock(["/"], []) == abi
    create = next(
        c for c in syscalls if c[0] == L._SYS_LANDLOCK_CREATE_RULESET and c[1] is not None
    )
    assert len(create[1].raw) == fields * 8


def test_apply_landlock_skips_rules_with_no_handled_rights(syscalls, monkeypatch) -> None:
    syscalls.abi["value"] = 1
    monkeypatch.setattr(L, "_READ", 1 << 20)  # a right this ABI does not handle
    L.apply_landlock(["/"], [])
    assert _rules(syscalls) == []


def test_apply_seccomp_installs_filter(syscalls) -> None:
    L.apply_seccomp("x86_64")
    nr = L._ARCH["x86_64"][1]["seccomp"]
    call = next(c for c in syscalls if c[0] == nr)
    assert call[1] == L._SECCOMP_SET_MODE_FILTER and call[2] == L._SECCOMP_FILTER_FLAG_TSYNC
    assert ("no_new_privs",) in syscalls


def test_build_filter_unknown_arch() -> None:
    with pytest.raises(L.ConfinementError, match="no seccomp filter"):
        L.build_filter("sparc")


def test_build_filter_jump_out_of_range(monkeypatch) -> None:
    monkeypatch.setattr(L, "_DENIED", tuple(f"x{i}" for i in range(300)) + ("execve",))
    arch, nrs = L._ARCH["aarch64"]
    many = dict(nrs, **{f"x{i}": 1000 + i for i in range(300)})
    monkeypatch.setitem(L._ARCH, "aarch64", (arch, many))
    with pytest.raises(L.ConfinementError, match="out of range"):
        L.build_filter("aarch64")


def test_confine_non_linux(monkeypatch) -> None:
    monkeypatch.setattr(L.sys, "platform", "darwin")
    assert L.confine([], []) == [
        "no kernel confinement on darwin; only the audit hook applies, "
        "and there is no memory limit (RLIMIT_AS is Linux-only)"
    ]


def test_confine_landlock_optional_seccomp_required(monkeypatch) -> None:
    monkeypatch.setattr(L.sys, "platform", "linux")

    def no_landlock(*a: Any) -> int:
        raise L.ConfinementError("Landlock is not supported by this kernel")

    applied: list[str] = []
    monkeypatch.setattr(L, "apply_landlock", no_landlock)
    monkeypatch.setattr(L, "apply_seccomp", lambda: applied.append("seccomp"))
    warnings = L.confine(["/"], ["/tmp"])
    assert applied == ["seccomp"]
    assert len(warnings) == 1 and "Landlock unavailable" in warnings[0]

    def no_seccomp() -> None:
        raise OSError(1, "EPERM")

    monkeypatch.setattr(L, "apply_landlock", lambda r, w: 6)
    monkeypatch.setattr(L, "apply_seccomp", no_seccomp)
    with pytest.raises(L.ConfinementError, match="could not install the seccomp filter"):
        L.confine(["/"], ["/tmp"])


def test_raw_syscall_and_prctl_errors(monkeypatch) -> None:
    """The ctypes wrappers translate a -1 return into OSError with errno."""

    class FakeLibc:
        def syscall(self, *a: Any) -> int:
            return -1

        def prctl(self, *a: Any) -> int:
            return -1

    monkeypatch.setattr(L, "_libc", FakeLibc())
    monkeypatch.setattr(L.ctypes, "get_errno", lambda: 1)
    with pytest.raises(OSError) as e1:
        L._syscall(0)
    with pytest.raises(OSError) as e2:
        L._no_new_privs()
    assert e1.value.errno == 1 and e2.value.errno == 1


# --- the seccomp program, executed --------------------------------------------------------
#
# A tiny classic-BPF interpreter runs the program build_filter() emits against a synthetic
# ``struct seccomp_data``, so every rule (including the ones added after the escape fuzzer,
# tests/sandbox/test_fuzz_escape.py) is checked on both architectures without loading it.

_ALLOW = "allow"
_EPERM = "EPERM"
_ENOSYS = "ENOSYS"
_PID = 4242


def _run_bpf(prog: bytes, *, arch: int, nr: int, args: tuple[int, ...] = ()) -> str:
    args = tuple(args) + (0,) * (6 - len(args))
    data = struct.pack("=iIQ6Q", nr, arch, 0, *(a & 0xFFFFFFFFFFFFFFFF for a in args))
    insns = [struct.unpack("=HBBI", prog[i : i + 8]) for i in range(0, len(prog), 8)]
    pc, acc = 0, 0
    for _ in range(len(insns)):  # forward jumps only: at most one pass
        code, jt, jf, k = insns[pc]
        if code == L._BPF_LD_W_ABS:
            (acc,) = struct.unpack_from("=I", data, k)
            pc += 1
        elif code in (L._BPF_JEQ, L._BPF_JGE, L._BPF_JSET):
            hit = {L._BPF_JEQ: acc == k, L._BPF_JGE: acc >= k, L._BPF_JSET: bool(acc & k)}[code]
            pc += 1 + (jt if hit else jf)
        elif code == L._BPF_RET:
            if k == L._RET_ALLOW:
                return _ALLOW
            assert k & 0xFFFF0000 == L._RET_ERRNO, hex(k)
            return {1: _EPERM, 38: _ENOSYS}[k & 0xFFFF]
        else:
            raise AssertionError(f"unexpected BPF opcode {code:#x}")
    raise AssertionError("program fell off the end")


@pytest.fixture(params=["x86_64", "aarch64"])
def bpf(request: pytest.FixtureRequest) -> Any:
    machine = request.param
    audit_arch, nrs = L._ARCH[machine]
    prog = L.build_filter(machine, pid=_PID)

    def run(name_or_nr: str | int, *args: int, arch: int = audit_arch) -> str:
        nr = nrs[name_or_nr] if isinstance(name_or_nr, str) else name_or_nr
        return _run_bpf(prog, arch=arch, nr=nr, args=args)

    run.machine, run.nrs, run.prog = machine, nrs, prog  # type: ignore[attr-defined]
    return run


def test_filter_size_matches_docs() -> None:
    # docs/sandbox.md and docs/sandbox-performance.md cite the x86_64 size.
    assert len(L.build_filter("x86_64", pid=1)) // 8 == 115
    assert len(L.build_filter("aarch64", pid=1)) // 8 == 103


def test_syscall_tables_are_consistent() -> None:
    for machine in ("x86_64", "aarch64"):
        nrs = L._ARCH[machine][1]
        assert len(set(nrs.values())) == len(nrs), f"duplicate syscall number on {machine}"
        assert all(n < L._FIRST_UNREVIEWED_NR for n in nrs.values())
        for name in (*L._SELF_ONLY, "clone", "clone3", "ioctl", "seccomp"):
            assert name in nrs
    # aarch64 only lacks the legacy syscalls that the generic table replaced with *at variants
    missing = {n for n in L._DENIED if n not in L._ARCH["aarch64"][1]}
    assert missing == {
        "fork", "vfork", "chmod", "chown", "lchown", "mknod",
        "utime", "utimes", "futimesat", "iopl", "ioperm",
    }  # fmt: skip
    assert set(L._DENIED) <= set(L._ARCH["x86_64"][1])


def test_every_denied_syscall_is_eperm(bpf: Any) -> None:
    denied = [n for n in L._DENIED if n in bpf.nrs]
    assert denied and all(bpf(n) == _EPERM for n in denied), [n for n in denied if bpf(n) != _EPERM]


@pytest.mark.parametrize("domain", [1, 2, 10, 16, 17])  # AF_UNIX, INET, INET6, NETLINK, PACKET
def test_every_socket_domain_is_refused(bpf: Any, domain: int) -> None:
    assert bpf("socket", domain, 1, 0) == _EPERM


def test_clone_threads_only_and_clone3_enosys(bpf: Any) -> None:
    thread_flags = 0x3D0F00  # glibc pthread_create: VM|FS|FILES|SIGHAND|THREAD|SYSVSEM|...
    assert bpf("clone", thread_flags) == _ALLOW
    assert bpf("clone", 0x11) == _EPERM  # fork(): SIGCHLD only
    assert bpf("clone", 0x00010000 | (1 << 40)) == _ALLOW  # only the low 32 bits are read
    assert bpf("clone3", 0) == _ENOSYS


@pytest.mark.parametrize("name", list(L._SELF_ONLY))
def test_signals_only_to_self(bpf: Any, name: str) -> None:
    assert bpf(name, _PID, 0) == _ALLOW
    assert bpf(name, _PID + 1, 0) == _EPERM
    assert bpf(name, 1, 0) == _EPERM
    assert bpf(name, 0, 0) == _EPERM  # kill(0, ...): the whole process group
    assert bpf(name, -1, 0) == _EPERM  # kill(-1, ...): every process we may signal


def test_ioctl_denylist(bpf: Any) -> None:
    for request in L._IOCTL_DENIED:
        assert bpf("ioctl", 3, request) == _EPERM, hex(request)
        assert bpf("ioctl", 3, request | (1 << 32)) == _EPERM  # high bits ignored by the kernel
    for request in (0x5401, 0x541B, 0x80086601):  # TCGETS, FIONREAD, FS_IOC_GETFLAGS
        assert bpf("ioctl", 3, request) == _ALLOW, hex(request)


def test_unreviewed_and_foreign_syscalls(bpf: Any) -> None:
    for nr in (L._FIRST_UNREVIEWED_NR, 469, 1000, 0x3FFFFFFF):
        assert bpf(nr) == _ENOSYS
    assert bpf(L._FIRST_UNREVIEWED_NR - 1) == _ALLOW
    assert bpf(0, arch=0x40000003) == _EPERM  # AUDIT_ARCH_I386
    if bpf.machine == "x86_64":
        assert bpf(L._X32_SYSCALL_BIT | 1) == _EPERM  # x32 ABI write()


def test_ordinary_syscalls_are_allowed(bpf: Any) -> None:
    # read, write, openat, mmap, futex, getpid, exit_group, rt_sigaction, landlock_*
    common = {
        "x86_64": (0, 1, 257, 9, 202, 39, 231, 13),
        "aarch64": (63, 64, 56, 222, 98, 172, 94, 134),
    }[bpf.machine]
    for nr in (*common, L._SYS_LANDLOCK_CREATE_RULESET, L._SYS_LANDLOCK_RESTRICT_SELF):
        assert bpf(nr) == _ALLOW, nr


def test_apply_seccomp_passes_a_program_scoped_to_this_pid(monkeypatch) -> None:
    seen: list[bytes] = []
    nr_seccomp = L._ARCH["x86_64"][1]["seccomp"]

    def fake_syscall(nr: int, *args: Any) -> int:
        if nr == nr_seccomp:
            fprog = args[2]._obj
            seen.append(L.ctypes.string_at(fprog.filter, fprog.len * 8))
        return 0

    monkeypatch.setattr(L, "_syscall", fake_syscall)
    monkeypatch.setattr(L, "_no_new_privs", lambda: None)
    L.apply_seccomp("x86_64")
    (prog,) = seen
    assert prog == L.build_filter("x86_64", pid=L.os.getpid())
    arch = L._ARCH["x86_64"][0]
    kill = L._ARCH["x86_64"][1]["kill"]
    assert _run_bpf(prog, arch=arch, nr=kill, args=(L.os.getpid(), 0)) == _ALLOW
    assert _run_bpf(prog, arch=arch, nr=kill, args=(L.os.getppid(), 0)) == _EPERM


def test_confine_records_phase_marks(monkeypatch) -> None:
    monkeypatch.setattr(L.sys, "platform", "linux")
    monkeypatch.setattr(L, "apply_landlock", lambda r, w: 6)
    monkeypatch.setattr(L, "apply_seccomp", lambda: None)
    marks: dict[str, float] = {}
    assert L.confine(["/"], ["/tmp"], marks=marks) == []
    assert set(marks) == {"landlock_done", "seccomp_done"}
    assert marks["landlock_done"] <= marks["seccomp_done"]
