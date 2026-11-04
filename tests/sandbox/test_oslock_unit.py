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
    assert L.confine([], []) == ["no kernel confinement on darwin; only the audit hook applies"]


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
