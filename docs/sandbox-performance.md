# Sandbox performance

What `run_sql` and `run_python` cost, cold and warm, on the benchmark datasets, and where the
time goes. The short version:

- **`run_sql` is cheap once the sandbox exists.** A query takes 1–11 ms. Building the sandbox
  (CSV → private DuckDB file → reopen read-only) costs 100–230 ms per dataset, and a fresh
  process pays about 0.5 s to import DuckDB and pandas.
- **`run_python` costs about 1–1.6 s per call, and almost all of it is start-up.** Every call
  starts a new interpreter, which imports pandas, numpy, matplotlib and scipy (~0.8–0.9 s,
  65–80 % of the call). Interpreter teardown adds 140–220 ms. Reloading the dataset from CSV
  in the parent adds 60–200 ms.
- **Kernel confinement is negligible.** Landlock plus seccomp take **1.4–2.2 ms** per call
  (Landlock ~0.7 ms, seccomp ~0.5 ms, loading `_oslock`/ctypes ~0.6 ms). That is about 0.15 %
  of a call. The seccomp filter (115 instructions on x86_64, 103 on aarch64) adds no
  measurable cost per syscall.
- **The audit hook makes each `open()` about 8 µs slower** (9.5 µs vs 1.2 µs), because it
  resolves every path with `os.path.realpath`. That only matters for code that opens
  thousands of files.

## How to reproduce

```bash
uv run python scripts/bench_sandbox.py                   # 5 repeats, all five datasets
```

```bash
uv run python scripts/bench_sandbox.py --repeats 10 --datasets ecommerce --json bench.json
```

The script prints the tables below as Markdown. `--skip-sql` and `--skip-python` run half of
it. The Python breakdown comes from an opt-in profiling hook, `python_runner._PROFILE`. When
the script sets it to a dict, the parent and the child record `time.perf_counter()` at each
phase boundary. On Linux that is `CLOCK_MONOTONIC`, so timestamps from the two processes
compare directly. It is `None` in normal use, and then nothing is recorded.

"Cold" and "warm" mean:

| Term | `run_sql` | `run_python` |
|---|---|---|
| process-cold | new interpreter: import the sandbox package (DuckDB, pandas), build the sandbox, first query | n/a |
| sandbox-cold / first | new `Sandbox` in a warm process, then its first query (cold DuckDB buffers) | the very first call on a host, with an empty matplotlib font cache |
| warm | the same query again on the same sandbox | any later call. There is no warm *process*: each call starts a new confined interpreter, so "warm" only means the OS page cache and the font cache are hot |

## Environment

The numbers below come from one run of the script, with medians over 5 repeats (warm queries:
20).

- 4 vCPU Linux VM, kernel 6.18, Landlock ABI 7, glibc 2.39
- Python 3.11.18, DuckDB 2.0.0, pandas 3.1.0, pyarrow 26.0.1
- Benchmark data from `benchmarks/data/*` (0.02–0.55 MB of CSV per dataset)

Absolute numbers will differ on other hardware. The proportions are what matter.

## `run_sql`

Process-cold, `count(*)` on each dataset's largest table (median ms):

| dataset | tables / rows / CSV MB | import sandbox | build sandbox (load) | 1st query | 2nd query | whole process |
|---|---|---|---|---|---|---|
| ecommerce | 6 / 9,963 / 0.55 | 464 | 228 | 5.9 | 1.6 | 892 |
| finance | 3 / 540 / 0.02 | 463 | 98 | 4.2 | 1.6 | 696 |
| hr | 5 / 6,815 / 0.38 | 492 | 190 | 4.4 | 2.6 | 834 |
| saas | 3 / 13,587 / 0.42 | 506 | 192 | 7.4 | 1.4 | 856 |
| support | 2 / 1,027 / 0.15 | 460 | 108 | 4.2 | 1.2 | 720 |

Per query, fresh sandbox vs warm sandbox (median / p90 ms):

| dataset | query | rows produced | first | warm |
|---|---|---|---|---|
| ecommerce | `count(*)` | 1 | 1.8 / 2.5 | 1.2 / 1.3 |
| ecommerce | `select *` (200-row cap) | 4,000 | 10.5 / 11.9 | 7.5 / 9.6 |
| finance | `count(*)` | 1 | 1.7 / 2.2 | 1.1 / 1.2 |
| finance | `select *` | 300 | 3.9 / 4.1 | 2.8 / 3.0 |
| hr | `count(*)` | 1 | 1.8 / 2.5 | 1.3 / 1.5 |
| hr | `select *` | 4,551 | 8.7 / 9.1 | 4.9 / 8.2 |
| saas | `count(*)` | 1 | 1.9 / 2.1 | 1.0 / 1.3 |
| saas | `select *` | 12,083 | 10.8 / 12.6 | 7.3 / 10.0 |
| support | `count(*)` | 1 | 2.2 / 2.3 | 1.3 / 1.5 |
| support | `select *` | 627 | 4.0 / 4.8 | 2.6 / 5.3 |

What dominates:

1. **Imports, once per process (~460–510 ms):** DuckDB, pandas and the sandbox package. The
   CLI pays this once. The API and the eval runner pay it at start-up, not per job.
2. **Building the sandbox, once per job (100–230 ms):** reading the CSVs and writing them
   into a private DuckDB file, then reopening it read-only. It grows with the data (roughly
   linearly in rows and tables).
3. **The queries themselves (1–11 ms):** the "first" overhead over "warm" is under 1 ms for
   aggregates. `select *` costs more because the runner still counts every row past the 200
   it keeps, and builds the Markdown preview. The statement allowlist, which parses each
   query twice (our tokenizer and DuckDB's parser), is inside the 1 ms floor.

## `run_python`

Cold host (empty font cache, no data):

| call | median ms | p90 ms |
|---|---|---|
| first call (builds the matplotlib font cache) | 2,248 | 2,348 |
| second call | 947 | 971 |

The font cache is built once per host and user, by a trusted process, into a private
directory (`python_runner._mpl_cache_dir`). After that every call is "warm".

Warm, phase breakdown (median ms; `ecommerce`, the largest dataset, and no data at all):

| phase | no data, `pass` | ecommerce, `pass` | ecommerce, `describe()` | ecommerce, groupby + histogram |
|---|---|---|---|---|
| parent: load CSVs + pickle them for the child | 0.5 | 168 | 198 | 195 |
| child: interpreter start-up | 29 | 25 | 32 | 26 |
| child: `sitecustomize` (rlimits, audit hook) | 4.5 | 3.6 | 3.8 | 3.7 |
| child: `site` → bootstrap | 5.3 | 4.7 | 6.8 | 6.0 |
| **child: import pandas / numpy / matplotlib / scipy** | **847** | **766** | **980** | **951** |
| child: unpickle data | 0.1 | 4.2 | 4.4 | 4.9 |
| child: namespace set-up | 0.1 | 0.1 | 0.1 | 0.1 |
| confine: import `_oslock` (ctypes, libc) | 0.5 | 0.6 | 0.6 | 0.7 |
| **confine: Landlock** | **0.6** | **0.8** | **0.8** | **0.9** |
| **confine: seccomp** | **0.4** | **0.5** | **0.6** | **0.5** |
| child: user code | 0.0 | 0.0 | 51 | 128 |
| child: save open figures | 0.2 | 0.3 | 0.2 | 101 |
| child: write result + interpreter teardown | 138 | 165 | 181 | 221 |
| parent: read output, export artifacts, delete temp dir | 2.3 | 1.7 | 2.6 | 2.4 |
| **total** | **1,023** | **1,159** | **1,468** | **1,635** |
| kernel confinement (sum of the three confine rows) | 1.4 | 1.9 | 2.1 | 2.1 |

The other four datasets look the same. Totals are 1.04–1.39 s, imports 770–900 ms, and
confinement 1.5–2.2 ms. Only "parent: load + pickle" scales with the data: 60 ms (finance)
to 205 ms (hr).

Of the import time, pandas takes ~300 ms, matplotlib with pyplot ~350–420 ms, numpy ~70 ms
and the top-level `scipy` package ~10 ms. Note that `from scipy import stats` **in user code**
costs another **1.6–2.0 s**: `scipy` is imported eagerly but its submodules are lazy, so a
snippet that touches `scipy.stats` takes roughly twice as long as one that doesn't.

### Per-syscall cost of the confinement

Measured inside a sandbox (best of 5 loops) vs the same loop in an unconfined interpreter:

| operation | unconfined | kernel layer only (seccomp + Landlock) | full sandbox (+ audit hook) |
|---|---|---|---|
| `getppid()` (cheapest syscall) | 195 ns | 163 ns | 159 ns |
| `stat()` of a file in the work dir | 1.10 µs | 0.97 µs | 1.01 µs |
| `open()` + `close()` in the work dir | 1.22 µs | 1.27 µs | **9.5 µs** |

The kernel layer is within noise: the BPF filter is a straight run of compares, and Landlock
checks are cheap on a short path. The audit hook is what costs, at about 8 µs per `open`,
because it calls `os.path.realpath` on the path. `stat` raises no audit event, so it pays
nothing.

## What dominates, and what would help

For `run_python`, the order is: library imports (~800–900 ms), then teardown (140–220 ms),
then reloading the data (60–200 ms). Everything else, including the security layers, is in
the noise. Possible improvements, none of them implemented here:

1. **Pre-imported zygote.** A long-lived, *unconfined but idle* process that has imported
   pandas, numpy, matplotlib and scipy, and forks one child per call. Each child then applies
   rlimits, the audit hook, Landlock and seccomp exactly as today. This would remove ~85 % of
   the latency (≈1.0 s → ≈0.15 s). Costs: the zygote must never run user code, children
   inherit its memory layout (ASLR is shared across calls), and forking from a process
   holding many threads or locks needs care. It is the only change that moves the needle.
2. **Exit with `os._exit(0)` after writing `result.json`.** Measured on the same imports:
   teardown goes from 134 ms to 9 ms. Caveat: it skips flushing files that user code left
   open, and those files are the artifacts. The bootstrap would have to flush and close them
   first (e.g. `gc.collect()` plus closing every `io` object it can find), or the change
   would silently truncate outputs.
3. **Cache the pickled tables per sandbox** instead of re-reading the CSVs on every call
   (this is review finding L20). That saves 60–200 ms per call on these datasets, and more on
   larger ones.
4. **Skip the eager `scipy` import** and let user code import it. This saves little (~10 ms),
   because `scipy` is already lazy. The real cost is `scipy.stats` (1.6–2.0 s), and a zygote
   (1) could pre-import it.

For `run_sql`, nothing stands out: queries are milliseconds, and the one-off sandbox build
scales with the data, as expected.

The kernel layers (Landlock + seccomp) are **not** a reason to avoid confinement: about 2 ms
per call, and no measurable cost per syscall.
