"""Benchmark the sandbox: ``run_sql`` and ``run_python``, cold vs warm, with a phase breakdown.

Usage::

    uv run python scripts/bench_sandbox.py                 # all benchmark datasets, 5 repeats
    uv run python scripts/bench_sandbox.py --repeats 10 --datasets ecommerce,saas
    uv run python scripts/bench_sandbox.py --json bench.json

What "cold" and "warm" mean here:

* **SQL, process-cold** - a fresh interpreter: ``import analyst_agent.sandbox`` (DuckDB,
  pandas), build the sandbox (CSV -> private DuckDB file -> reopen read-only), first query.
* **SQL, sandbox-cold** - in a warm process: building a new ``Sandbox`` for the dataset
  and its first query. **Warm** - the same query again on that sandbox.
* **Python, cold** - the very first ``run_python`` on a host, with an empty matplotlib font
  cache (it is built once by a trusted process, see ``python_runner._mpl_cache_dir``).
* **Python, warm** - every later call. There is no warm *process*: each call starts a new,
  confined interpreter, so "warm" only means the OS page cache and the font cache are hot.

The Python breakdown comes from ``python_runner._PROFILE``: the parent and the child record
``time.perf_counter()`` (CLOCK_MONOTONIC, comparable across processes on Linux) at each phase,
including Landlock and seccomp set-up. See docs/sandbox-performance.md for results.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "benchmarks" / "data"
DEFAULT_DATASETS = ("ecommerce", "finance", "hr", "saas", "support")

# Python phases as (label, start mark, end mark); the marks are set in python_runner.run_python,
# _child/sitecustomize.py, _child/bootstrap.py and _child/_oslock.confine.
PY_PHASES: list[tuple[str, str, str]] = [
    ("parent: load + pickle data", "parent_start", "spawn"),
    ("child: interpreter start-up", "spawn", "site_start"),
    ("child: sitecustomize (rlimits, audit hook)", "site_start", "site_end"),
    ("child: site/runpy to bootstrap", "site_end", "boot_start"),
    ("child: import pandas/numpy/mpl/scipy", "boot_start", "imports_done"),
    ("child: unpickle data", "imports_done", "data_loaded"),
    ("child: namespace set-up", "data_loaded", "confine_start"),
    ("confine: import _oslock (ctypes)", "confine_start", "oslock_imported"),
    ("confine: Landlock", "oslock_imported", "landlock_done"),
    ("confine: seccomp", "landlock_done", "seccomp_done"),
    ("child: user code", "user_start", "user_end"),
    ("child: save figures", "user_end", "figures_done"),
    ("child: write result + exit", "figures_done", "exited"),
    ("parent: read output, export artifacts, rm temp", "exited", "parent_end"),
]

PY_SNIPPETS = {
    "noop": "pass",
    "describe": "print(len(tables))\nfor t in tables.values():\n    t.describe()",
    "groupby+plot": (
        "num = [t for t in tables.values() if len(t.select_dtypes('number').columns)]\n"
        "t = max(num, key=len)\n"
        "c = t.select_dtypes('number').columns[0]\n"
        "t.groupby(t.columns[0])[c].agg(['mean', 'sum'])\n"
        "t[c].plot.hist(bins=20)\n"
        "print(t[c].mean())"
    ),
}


# --------------------------------------------------------------------------- helpers


def datasets_for(name: str) -> dict[str, Path]:
    files = sorted((DATA / name).glob("*.csv"))
    if not files:
        raise SystemExit(f"no CSV files in {DATA / name}")
    from analyst_agent.sandbox._loaders import safe_identifier

    return {safe_identifier(p.stem): p for p in files}


def stats(xs: list[float]) -> dict[str, float]:
    xs = sorted(xs)
    if not xs:
        return {"n": 0, "median": float("nan"), "p90": float("nan"), "min": float("nan")}
    p90 = xs[min(len(xs) - 1, int(round(0.9 * (len(xs) - 1))))]
    return {"n": len(xs), "median": statistics.median(xs), "p90": p90, "min": xs[0]}


def ms(x: float) -> str:
    return f"{x:,.1f}" if x < 100 else f"{x:,.0f}"


def table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


def sql_queries(sb: Any) -> dict[str, str]:
    """Representative queries on the dataset's largest table."""
    sizes = {t: sb.run_sql(f'SELECT count(*) AS n FROM "{t}"').rows[0]["n"] for t in sb.tables}
    big = max(sizes, key=lambda t: sizes[t])
    cols = sb.run_sql(f'DESCRIBE "{big}"').rows
    text = next((c["column_name"] for c in cols if c["column_type"] == "VARCHAR"), None)
    num = next(
        (
            c["column_name"]
            for c in cols
            if c["column_type"] in ("BIGINT", "INTEGER", "DOUBLE", "DECIMAL(18,3)")
        ),
        None,
    )
    q = {
        "count(*)": f'SELECT count(*) FROM "{big}"',
        "select * (200-row cap)": f'SELECT * FROM "{big}"',
    }
    if text and num:
        q["group by + avg"] = (
            f'SELECT "{text}", count(*) AS n, avg("{num}") AS a FROM "{big}" '
            f"GROUP BY 1 ORDER BY n DESC"
        )
    return q


# --------------------------------------------------------------------------- SQL


_SQL_COLD_CHILD = r"""
import json, sys, time
t0 = time.perf_counter()
from analyst_agent import sandbox
t1 = time.perf_counter()
sb = sandbox.create_sandbox({k: v for k, v in json.loads(sys.argv[1]).items()})
t2 = time.perf_counter()
r = sb.run_sql(sys.argv[2])
t3 = time.perf_counter()
r2 = sb.run_sql(sys.argv[2])
t4 = time.perf_counter()
assert r.ok and r2.ok, (r.error, r2.error)
print(json.dumps({"import": t1 - t0, "load": t2 - t1, "first": t3 - t2, "second": t4 - t3}))
"""


def bench_sql_process_cold(name: str, repeats: int) -> dict[str, dict[str, float]]:
    ds = {k: str(v) for k, v in datasets_for(name).items()}
    big = max(ds, key=lambda k: Path(ds[k]).stat().st_size)
    query = f'SELECT count(*) FROM "{big}"'
    runs: dict[str, list[float]] = {"import": [], "load": [], "first": [], "second": []}
    total: list[float] = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        out = subprocess.run(
            [sys.executable, "-c", _SQL_COLD_CHILD, json.dumps(ds), query],
            capture_output=True,
            text=True,
            check=True,
        )
        total.append((time.perf_counter() - t0) * 1000)
        for k, v in json.loads(out.stdout).items():
            runs[k].append(v * 1000)
    res = {k: stats(v) for k, v in runs.items()}
    res["process total"] = stats(total)
    return res


def bench_sql(name: str, repeats: int) -> dict[str, Any]:
    from analyst_agent import sandbox

    ds = datasets_for(name)
    rows = sum(sum(1 for _ in p.open()) - 1 for p in ds.values())
    mb = sum(p.stat().st_size for p in ds.values()) / 1e6
    load: list[float] = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        sb = sandbox.create_sandbox(ds)
        load.append((time.perf_counter() - t0) * 1000)
        sb.close()

    queries: dict[str, dict[str, Any]] = {}
    sb = sandbox.create_sandbox(ds)
    try:
        for label, q in sql_queries(sb).items():
            # first run of this query on a fresh sandbox (cold DuckDB buffer pool)
            first: list[float] = []
            for _ in range(repeats):
                fresh = sandbox.create_sandbox(ds)
                t0 = time.perf_counter()
                r = fresh.run_sql(q)
                first.append((time.perf_counter() - t0) * 1000)
                fresh.close()
                assert r.ok, r.error
            warm: list[float] = []
            sb.run_sql(q)
            for _ in range(max(repeats * 4, 10)):
                t0 = time.perf_counter()
                r = sb.run_sql(q)
                warm.append((time.perf_counter() - t0) * 1000)
            queries[label] = {"first": stats(first), "warm": stats(warm), "rows": r.row_count}
    finally:
        sb.close()
    return {
        "tables": len(ds),
        "rows": rows,
        "csv_mb": mb,
        "load": stats(load),
        "queries": queries,
        "process_cold": bench_sql_process_cold(name, max(2, repeats // 2)),
    }


# --------------------------------------------------------------------------- Python


def run_profiled(code: str, data: Any, artifacts: Path) -> dict[str, float]:
    from analyst_agent.sandbox import python_runner

    prof: dict[str, float] = {}
    python_runner._PROFILE = prof
    try:
        r = python_runner.run_python(code, data, timeout_s=120, artifacts_dir=artifacts)
    finally:
        python_runner._PROFILE = None
    if not r.ok:
        raise RuntimeError(f"benchmark snippet failed: {r.error}\n{r.stderr}")
    return dict(prof)


def phases(prof: dict[str, float]) -> dict[str, float]:
    out = {
        label: (prof[b] - prof[a]) * 1000 for label, a, b in PY_PHASES if a in prof and b in prof
    }
    out["total"] = (prof["parent_end"] - prof["parent_start"]) * 1000
    if "oslock_imported" in prof and "seccomp_done" in prof:
        out["kernel confinement (sum)"] = (prof["seccomp_done"] - prof["confine_start"]) * 1000
    return out


def bench_python(names: list[str], repeats: int, artifacts: Path) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for name in ["(no data)"] + names:
        data = None if name == "(no data)" else datasets_for(name)
        per_snippet: dict[str, Any] = {}
        for label, code in PY_SNIPPETS.items():
            if data is None and label != "noop":
                continue
            run_profiled(code, data, artifacts)  # warm-up (page cache)
            samples = [phases(run_profiled(code, data, artifacts)) for _ in range(repeats)]
            keys = samples[0].keys()
            per_snippet[label] = {k: stats([s[k] for s in samples if k in s]) for k in keys}
        results[name] = per_snippet
    return results


_PY_COLD_CHILD = r"""
import json, sys, time
from pathlib import Path
from analyst_agent.sandbox import python_runner
out = {}
for i in range(2):
    t0 = time.perf_counter()
    r = python_runner.run_python("plt.plot([1, 2]); print(1)", None, artifacts_dir=sys.argv[1])
    out[f"call{i + 1}"] = (time.perf_counter() - t0) * 1000
    assert r.ok, r.error
print(json.dumps(out))
"""


def bench_python_cold(repeats: int, artifacts: Path) -> dict[str, dict[str, float]]:
    """First run_python on a fresh host: empty matplotlib font cache (fresh TMPDIR)."""
    runs: dict[str, list[float]] = {"call1": [], "call2": []}
    for _ in range(repeats):
        with tempfile.TemporaryDirectory(prefix="bench-cold-") as tmp:
            env = dict(os.environ, TMPDIR=tmp)
            out = subprocess.run(
                [sys.executable, "-c", _PY_COLD_CHILD, str(artifacts)],
                capture_output=True,
                text=True,
                check=True,
                env=env,
            )
        for k, v in json.loads(out.stdout).items():
            runs[k].append(v)
    return {
        "first call (builds font cache)": stats(runs["call1"]),
        "second call": stats(runs["call2"]),
    }


# --------------------------------------------------------------------------- report


def environment() -> dict[str, Any]:
    sys.path.insert(0, str(REPO / "src" / "analyst_agent" / "sandbox" / "_child"))
    try:
        import _oslock

        abi = _oslock.landlock_abi()
    finally:
        sys.path.pop(0)
    import duckdb
    import pandas

    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpus": os.cpu_count(),
        "landlock_abi": abi,
        "duckdb": duckdb.__version__,
        "pandas": pandas.__version__,
    }


def render(res: dict[str, Any]) -> str:
    lines = ["## Environment", ""]
    lines += [f"- {k}: `{v}`" for k, v in res["env"].items()]
    lines += [f"- repeats: {res['repeats']}", ""]

    if res["sql"]:
        lines += render_sql(res)
    if "python_cold" in res:
        lines += render_python(res)
    return "\n".join(lines)


def render_sql(res: dict[str, Any]) -> list[str]:
    lines = ["## run_sql", ""]
    rows = []
    for name, r in res["sql"].items():
        pc = r["process_cold"]
        rows.append(
            [
                name,
                f"{r['tables']} / {r['rows']:,} / {r['csv_mb']:.2f}",
                ms(pc["import"]["median"]),
                ms(r["load"]["median"]),
                ms(pc["first"]["median"]),
                ms(pc["second"]["median"]),
                ms(pc["process total"]["median"]),
            ]
        )
    lines += [
        "Process-cold, `count(*)` on the largest table (median ms):",
        "",
        table(
            [
                "dataset",
                "tables / rows / CSV MB",
                "import sandbox",
                "load (in warm process)",
                "1st query",
                "2nd query",
                "process total",
            ],
            rows,
        ),
        "",
        "Per query, fresh sandbox vs warm sandbox (median / p90 ms):",
        "",
    ]
    rows = []
    for name, r in res["sql"].items():
        for q, v in r["queries"].items():
            rows.append(
                [
                    name,
                    q,
                    f"{v['rows']:,}",
                    f"{ms(v['first']['median'])} / {ms(v['first']['p90'])}",
                    f"{ms(v['warm']['median'])} / {ms(v['warm']['p90'])}",
                ]
            )
    lines += [table(["dataset", "query", "rows", "first", "warm"], rows), ""]
    return lines


def render_python(res: dict[str, Any]) -> list[str]:
    lines = ["## run_python", "", "Cold host (empty font cache), no data (median ms):", ""]
    lines += [
        table(
            ["call", "median", "p90"],
            [[k, ms(v["median"]), ms(v["p90"])] for k, v in res["python_cold"].items()],
        ),
        "",
    ]
    for name, per in res["python"].items():
        labels = list(per)
        keys: list[str] = []
        for v in per.values():
            keys += [k for k in v if k not in keys]
        rows = [
            [k] + [ms(per[s][k]["median"]) if k in per[s] else "" for s in labels] for k in keys
        ]
        lines += [
            f"Warm, dataset **{name}**, phase breakdown (median ms):",
            "",
            table(["phase"] + labels, rows),
            "",
        ]
    return lines


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--datasets", default=",".join(DEFAULT_DATASETS))
    ap.add_argument("--json", type=Path, help="also write raw results here")
    ap.add_argument("--skip-sql", action="store_true")
    ap.add_argument("--skip-python", action="store_true")
    args = ap.parse_args()
    names = [n for n in args.datasets.split(",") if n]

    res: dict[str, Any] = {"env": environment(), "repeats": args.repeats, "sql": {}, "python": {}}
    with tempfile.TemporaryDirectory(prefix="bench-artifacts-") as tmp:
        artifacts = Path(tmp)
        if not args.skip_sql:
            for n in names:
                print(f"[bench] sql {n}", file=sys.stderr)
                res["sql"][n] = bench_sql(n, args.repeats)
        if not args.skip_python:
            print("[bench] python cold", file=sys.stderr)
            res["python_cold"] = bench_python_cold(max(2, args.repeats // 2), artifacts)
            print("[bench] python warm", file=sys.stderr)
            res["python"] = bench_python(names, args.repeats, artifacts)
    if args.json:
        args.json.write_text(json.dumps(res, indent=2))
    print(render(res))


if __name__ == "__main__":
    main()
