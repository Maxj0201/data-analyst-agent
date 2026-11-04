"""Branch tests for the sandbox runners that the end-to-end security tests do not reach.

The Python runner's exit-status handling is exercised with a fake ``Popen`` so each outcome
(timeout race, CPU kill, OOM, crash, corrupt result, failed artifact export) is deterministic
and fast; the real subprocess path is covered by ``test_python_security.py``.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import pytest

from analyst_agent import sandbox
from analyst_agent.sandbox import DuckDBRunner, python_runner
from analyst_agent.sandbox import _errors as T
from analyst_agent.sandbox._loaders import (
    DataLoadError,
    load_dataframes,
    load_into_duckdb,
    normalize_sources,
)
from analyst_agent.sandbox.duckdb_runner import (
    StatementNotAllowed,
    _dedupe,
    _jsonable,
    to_markdown,
    validate_query,
)

# --------------------------------------------------------------------------- _errors


def test_kind_of() -> None:
    assert T.kind_of(None) is None
    assert T.kind_of("") is None
    assert T.kind_of("Timeout: too slow") == "Timeout"
    assert T.kind_of("NoColon") == "NoColon"


# --------------------------------------------------------------------------- _loaders


def test_normalize_sources_shapes(tmp_path: Path) -> None:
    csv = tmp_path / "My Data.csv"
    csv.write_text("a\n1\n")
    assert normalize_sources(None) == {}
    assert normalize_sources(csv) == {"my_data": csv.resolve()}
    assert normalize_sources(str(csv)) == {"my_data": csv.resolve()}
    assert normalize_sources({"2024 sales": csv}) == {"t_2024_sales": csv.resolve()}


def test_normalize_sources_errors(tmp_path: Path) -> None:
    a = tmp_path / "a.csv"
    a.write_text("x\n1\n")
    (tmp_path / "sub").mkdir()
    b = tmp_path / "sub" / "a.csv"
    b.write_text("x\n2\n")
    bad = tmp_path / "a.xlsx"
    bad.write_text("nope")
    with pytest.raises(DataLoadError, match="not found"):
        normalize_sources([tmp_path / "missing.csv"])
    with pytest.raises(DataLoadError, match="unsupported file type"):
        normalize_sources([bad])
    with pytest.raises(DataLoadError, match="duplicate table name"):
        normalize_sources([a, b])  # both stems are "a"


def test_load_into_duckdb_rejects_clashing_sqlite_tables(tmp_path: Path) -> None:
    db = tmp_path / "shop.sqlite"
    with sqlite3.connect(db) as con:
        pd.DataFrame({"x": [1]}).to_sql("orders", con, index=False)
    csv = tmp_path / "orders.csv"
    csv.write_text("x\n2\n")
    con = duckdb.connect(":memory:")
    try:
        with pytest.raises(DataLoadError, match="duplicate table name 'orders'"):
            load_into_duckdb(con, {"orders": csv, "shop": db})
    finally:
        con.close()


def test_load_dataframes_parquet_and_sqlite(tmp_path: Path) -> None:
    pq = tmp_path / "p.parquet"
    pd.DataFrame({"v": [1, 2, 3]}).to_parquet(pq)
    db = tmp_path / "s.sqlite"
    with sqlite3.connect(db) as con:
        pd.DataFrame({"w": ["a"]}).to_sql("letters", con, index=False)
    frames = load_dataframes({"p": pq, "s": db})
    assert list(frames["p"]["v"]) == [1, 2, 3]
    assert list(frames["letters"]["w"]) == ["a"]


# --------------------------------------------------------------------------- duckdb_runner


def test_validate_query_parse_error_and_explain_targets() -> None:
    con = duckdb.connect(":memory:")
    try:
        with pytest.raises(StatementNotAllowed, match="Empty"):
            validate_query(con, "   ")
        with pytest.raises(StatementNotAllowed, match="Could not parse"):
            validate_query(con, "SELECT FROM WHERE (")
        with pytest.raises(StatementNotAllowed, match="Exactly one statement"):
            validate_query(con, "SELECT 1; SELECT 2")
        validate_query(con, "EXPLAIN ANALYZE SELECT 1")
        validate_query(con, "/* c */ -- x\n((SELECT 1))")
    finally:
        con.close()


def test_jsonable_markdown_and_dedupe() -> None:
    from decimal import Decimal

    assert _jsonable(float("nan")) == "nan"
    assert _jsonable(1.5) == 1.5
    assert _jsonable(Decimal("2.5")) == 2.5
    assert _jsonable(b"\x01\xff") == "01ff"
    assert _jsonable([1, 2]) == "[1, 2]"
    assert to_markdown([], []) == "_(no columns)_"
    assert to_markdown(["a"], [], total=0).endswith("_(0 rows)_")
    long = to_markdown(["a"], [{"a": "x" * 200 + "|\n"}])
    assert "..." in long and "\n|" not in long.split("\n", 2)[2]
    assert _dedupe(["a", "a", "b", "a"]) == ["a", "a_1", "b", "a_2"]


def test_runner_reload_closed_and_query_errors(data_files: dict[str, Path], tmp_path) -> None:
    r = DuckDBRunner(data_files)
    try:
        extra = tmp_path / "extra.csv"
        extra.write_text("k\n1\n")
        assert sorted(r.load({"extra": extra})) == ["extra", "orders"]  # rebuilds the file
        err = r.run_sql("SELECT nope FROM orders")
        assert not err.ok and T.kind_of(err.error) == T.QUERY_ERROR
        dup = r.run_sql("SELECT 1 AS a, 2 AS a")
        assert dup.columns == ["a", "a_1"]
    finally:
        r.close()
    closed = r.run_sql("SELECT 1")
    assert not closed.ok and T.kind_of(closed.error) == T.NO_DATA


def test_runner_maps_engine_exceptions(data_files: dict[str, Path]) -> None:
    """Permission / OOM / generic-after-timeout errors map to their stable kinds."""
    import threading

    r = DuckDBRunner(data_files)
    real_conn = r._conn
    assert real_conn is not None

    class Conn:
        def __init__(self, exc: Exception, fire_timer: bool) -> None:
            self.exc, self.fire_timer = exc, fire_timer

        def cursor(self) -> Any:
            outer, cur = self, real_conn.cursor()

            class Cur:
                def __getattr__(self, name: str) -> Any:
                    return getattr(cur, name)

                def execute(self, q: str) -> None:
                    if outer.fire_timer:  # the watchdog fires just before the error surfaces
                        for t in threading.enumerate():
                            if isinstance(t, threading.Timer):
                                t.function()  # type: ignore[attr-defined]
                    raise outer.exc

            return Cur()

    cases = [
        (duckdb.PermissionException("nope"), False, T.PERMISSION_DENIED),
        (duckdb.OutOfMemoryException("oom"), False, T.MEMORY_LIMIT),
        (duckdb.Error("cancelled"), True, T.TIMEOUT),
    ]
    try:
        for exc, fire, kind in cases:
            r._conn = Conn(exc, fire)  # type: ignore[assignment]
            res = r.run_sql("SELECT 1")
            assert not res.ok and T.kind_of(res.error) == kind, res.error
    finally:
        r._conn = real_conn
        r.close()


# --------------------------------------------------------------------------- Sandbox facade


def test_sandbox_facade_and_module_level(data_files: dict[str, Path], tmp_path: Path) -> None:
    with sandbox.create_sandbox(data_files, artifacts_dir=tmp_path / "a") as sb:
        assert set(sb.datasets) == {"orders"}
        extra = tmp_path / "more.csv"
        extra.write_text("z\n1\n")
        assert sorted(sb.add_data({"more": extra})) == ["more", "orders"]
    sandbox.reset()
    assert sandbox.loaded_tables() == []
    try:
        sandbox.load_data(data_files, artifacts_dir=tmp_path / "a1")
        extra2 = tmp_path / "again.csv"
        extra2.write_text("q\n1\n")
        tables = sandbox.load_data({"again": extra2}, artifacts_dir=tmp_path / "a2", replace=False)
        assert sorted(tables) == ["again", "orders"]
        assert sandbox._default is not None
        assert sandbox._default.artifacts_dir == tmp_path / "a2"
        sandbox.load_data({"again": extra2}, artifacts_dir=None, replace=False)
        assert sandbox._default.artifacts_dir == tmp_path / "a2"  # kept
    finally:
        sandbox.reset()


# --------------------------------------------------------------------------- python_runner helpers


def test_read_capped(tmp_path: Path) -> None:
    assert python_runner._read_capped(tmp_path / "missing") == ""
    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * (python_runner.MAX_OUTPUT_BYTES + 10))
    out = python_runner._read_capped(big)
    assert out.endswith("bytes ...]") and out.startswith("x" * 100)


def test_export_artifacts_filters(tmp_path: Path, monkeypatch) -> None:
    work = tmp_path / "work"
    (work / "__sandbox__" / "figures").mkdir(parents=True)
    (work / "__sandbox__" / "figures" / "fig_0.png").write_bytes(b"png")
    (work / "keep.csv").write_text("a\n")
    (work / "skip.exe").write_text("bin")
    (work / "huge.txt").write_text("x" * 50)
    os.symlink(work / "keep.csv", work / "link.csv")
    monkeypatch.setattr(python_runner, "MAX_FILE_BYTES", 10)
    out = python_runner._export_artifacts(work, tmp_path / "art")
    names = [Path(p).name for p in out]
    assert len(names) == 2
    assert names[0].endswith("fig_0.png") and names[1].endswith("keep.csv")

    for i in range(5):
        (work / f"f{i}.md").write_text("m")
    monkeypatch.setattr(python_runner, "MAX_ARTIFACTS", 3)
    assert len(python_runner._export_artifacts(work, tmp_path / "art2")) == 3


def test_prepare_data_variants(tmp_path: Path) -> None:
    csv = tmp_path / "t.csv"
    csv.write_text("a\n1\n")
    python_runner._prepare_data(None, tmp_path / "none")
    assert not (tmp_path / "none").exists()
    python_runner._prepare_data({"My Frame": pd.DataFrame({"b": [1]}), "t": csv}, tmp_path / "m")
    assert sorted(p.name for p in (tmp_path / "m").iterdir()) == ["my_frame.pkl", "t.pkl"]
    python_runner._prepare_data([csv], tmp_path / "l")
    assert [p.name for p in (tmp_path / "l").iterdir()] == ["t.pkl"]


def test_mpl_cache_dir_untrusted(tmp_path: Path, monkeypatch) -> None:
    python_runner._mpl_cache_dir.cache_clear()
    try:
        monkeypatch.setattr(python_runner.tempfile, "gettempdir", lambda: str(tmp_path))
        d = tmp_path / f"analyst-sandbox-mpl-{os.getuid()}"
        d.mkdir(mode=0o777)
        os.chmod(d, 0o777)  # group/world writable: someone else could poison it
        assert python_runner._mpl_cache_dir() is None

        python_runner._mpl_cache_dir.cache_clear()
        os.chmod(d, 0o700)
        calls: list[Any] = []
        monkeypatch.setattr(python_runner.subprocess, "run", lambda *a, **k: calls.append(a))
        assert python_runner._mpl_cache_dir() == str(d)
        assert calls  # no font cache yet -> built once by the trusted parent

        python_runner._mpl_cache_dir.cache_clear()

        def boom(*a: Any, **k: Any) -> None:
            raise subprocess.TimeoutExpired("python", 120)

        monkeypatch.setattr(python_runner.subprocess, "run", boom)
        assert python_runner._mpl_cache_dir() is None
    finally:
        python_runner._mpl_cache_dir.cache_clear()


# --------------------------------------------------------------------------- run_python outcomes


class _FakeProc:
    """Stands in for the sandbox child: optionally writes files, then 'exits'."""

    script: dict[str, Any] = {}

    def __init__(self, argv: list[str], *, cwd: Path, stdout: Any, stderr: Any, **_: Any) -> None:
        self.pid = 4242
        meta = Path(cwd) / "__sandbox__"
        s = type(self).script
        if "result" in s:
            (meta / "result.json").write_text(s["result"])
        if "stderr" in s:
            stderr.write(s["stderr"].encode())
        self._timeout = s.get("timeout", False)
        self._code = s.get("code", 0)

    def wait(self, timeout: float | None = None) -> int:
        if self._timeout and timeout is not None:
            raise subprocess.TimeoutExpired("child", timeout)
        return self._code


@pytest.fixture()
def fake_child(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(python_runner.subprocess, "Popen", _FakeProc)
    monkeypatch.setattr(python_runner, "_child_env", lambda *a, **k: {})

    def run(script: dict[str, Any], **kw: Any):
        _FakeProc.script = script
        return python_runner.run_python("print(1)", artifacts_dir=tmp_path / "art", **kw)

    return run


def test_run_python_timeout_with_vanished_group(fake_child, monkeypatch) -> None:
    def gone(pid: int, sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(python_runner.os, "killpg", gone)
    res = fake_child({"timeout": True, "code": -9}, timeout_s=0.5)
    assert not res.ok and T.kind_of(res.error) == T.TIMEOUT
    assert "0.5s" in (res.error or "")


@pytest.mark.parametrize(
    ("script", "kind"),
    [
        ({"code": -signal.SIGXCPU}, T.TIMEOUT),
        ({"code": -signal.SIGKILL}, T.TIMEOUT),
        ({"code": 1, "stderr": "MemoryError\n"}, T.MEMORY_LIMIT),
        ({"code": 3}, T.PROCESS_CRASHED),
        ({"code": 0, "result": "{not json"}, T.PROCESS_CRASHED),
        ({"code": 0, "result": "[1, 2]"}, T.PROCESS_CRASHED),
    ],
)
def test_run_python_exit_without_result(fake_child, script: dict[str, Any], kind: str) -> None:
    res = fake_child(script)
    assert not res.ok and T.kind_of(res.error) == kind, res.error


def test_run_python_error_payload(fake_child) -> None:
    payload = {
        "ok": False,
        "error": {"type": T.MEMORY_LIMIT, "message": "big", "traceback": "Traceback: here"},
    }
    res = fake_child({"code": 1, "result": json.dumps(payload), "stderr": "warn"})
    assert res.error == f"{T.MEMORY_LIMIT}: Out of memory (limit 1024 MB): big"
    assert res.stderr == "warn\nTraceback: here"

    res = fake_child({"code": 1, "result": json.dumps({"ok": False, "error": "flat"})})
    assert res.error == "Error: "


def test_run_python_artifact_export_failure(fake_child, monkeypatch) -> None:
    def broken(*a: Any) -> list[str]:
        raise OSError("disk full")

    monkeypatch.setattr(python_runner, "_export_artifacts", broken)
    res = fake_child({"code": 0, "result": json.dumps({"ok": True})})
    assert res.ok and res.artifacts == []
    assert "could not export artifacts: disk full" in res.stderr


def test_run_python_bad_data_context(tmp_path: Path) -> None:
    res = python_runner.run_python("1", [tmp_path / "missing.csv"], artifacts_dir=tmp_path)
    assert not res.ok and T.kind_of(res.error) == T.DATA_LOAD_ERROR


# --------------------------------------------------------------------------- phase profiling
# python_runner._PROFILE is set only by scripts/bench_sandbox.py; with it off nothing is recorded.

_PARENT_MARKS = {"parent_start", "spawn", "exited", "exported", "parent_end"}


def test_run_python_profile_off_by_default(fake_child) -> None:
    assert python_runner._PROFILE is None
    assert fake_child({"code": 0, "result": json.dumps({"ok": True})}).ok


def test_run_python_profile_merges_numeric_child_marks(fake_child, monkeypatch) -> None:
    prof: dict[str, float] = {"stale": 1.0}
    monkeypatch.setattr(python_runner, "_PROFILE", prof)
    child = {"site_start": 1.5, "user_done": 2, "bad": "x", "nested": {"a": 1}}
    res = fake_child({"code": 0, "result": json.dumps({"ok": True, "profile": child})})
    assert res.ok
    assert set(prof) == _PARENT_MARKS | {"site_start", "user_done"}  # stale cleared
    assert prof["user_done"] == 2.0 and isinstance(prof["user_done"], float)
    assert prof["parent_start"] <= prof["spawn"] <= prof["exited"] <= prof["exported"]
    assert prof["exported"] <= prof["parent_end"]


def test_run_python_profile_without_child_payload(fake_child, monkeypatch) -> None:
    prof: dict[str, float] = {}
    monkeypatch.setattr(python_runner, "_PROFILE", prof)
    assert not fake_child({"code": 3}).ok  # crashed: no result.json
    assert set(prof) == _PARENT_MARKS


def test_run_python_profile_real_child_reports_confinement_phases(monkeypatch) -> None:
    prof: dict[str, float] = {}
    monkeypatch.setattr(python_runner, "_PROFILE", prof)
    res = python_runner.run_python("print('hi')", timeout_s=60)
    assert res.ok, res.error
    assert {"site_start", "site_end", "boot_start", "seccomp_done"} <= set(prof)
    assert prof["spawn"] < prof["site_start"] < prof["seccomp_done"] < prof["exited"]
