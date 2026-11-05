# Fresh-clone audit: three reviewer machines

A follow-up to [final-review.md](final-review.md): the same cold read, done as three different
reviewers who each clone the repository and follow the obvious path. Every clone was a new
`git clone` into an empty directory. "Before" is `main` at `011c0b6`; "after" is this branch
(`chore/fresh-clone-audit`).

| Clone | Install | Python |
|---|---|---|
| **A** | `uv sync` (no extras: what someone who skims the README does) | 3.11.18 (`.python-version`) |
| **B** | `uv sync --all-extras --no-extra embeddings` (the README quickstart, `make install`, CI) | 3.11.18 |
| **C** | same as B, with `UV_PYTHON=3.12` | 3.12.3 (`/usr/bin/python3.12`) |

In each clone I ran: `make help`, `make lint`, `make test`,
`uv run analyst-agent ask --dry-run --data benchmarks/data/saas/subscriptions.csv "which region
has the highest mrr"`, `uv run python -m analyst_agent.eval run --agent oracle --no-judge --limit
5`, `uv run analyst-agent-mcp --help` and `scripts/preflight.sh --dry-run`. uv 0.11.32, Linux.

## Results at a glance

| Command | A before | A after | B before | B after | C before | C after |
|---|---|---|---|---|---|---|
| `make help` | ok | ok | ok | ok | ok | ok |
| `make lint` | ok | ok | ok | ok | ok | ok |
| `make test` | **exit 2: 2 collection errors** | ok: 958 passed, 8 skipped | ok: 1015 passed | ok: 1027 passed | ok: 1015 passed | ok: 1027 passed |
| `ask --dry-run` | ok | ok | ok | ok | ok | ok |
| `eval run --agent oracle --no-judge --limit 5` | ok (4/4, L3 skipped) | ok | ok | ok | ok | ok |
| `analyst-agent-mcp --help` | **exit 1: traceback** | exit 2: one-line hint | ok | ok | ok | ok |
| `scripts/preflight.sh --dry-run` | **exit 1: 4 FAIL** | exit 1: 3 FAIL (see O1) | **exit 1: FAIL `[embeddings]`** | ok (2 WARN) | **exit 1: FAIL `[embeddings]`** | ok (2 WARN) |
| `make check` | – | – | – | **green** | – | – |

Python 3.12 behaves exactly like 3.11: same counts, same output, no new warnings.

## Findings

### F1. Plain `uv sync`: `make test` errors at collection (fixed)

This was final-review N9. In clone A, before:

```
__________________________ ERROR collecting tests/api __________________________
tests/api/conftest.py:9: in <module>
    import httpx
E   ModuleNotFoundError: No module named 'httpx'
_________ ERROR collecting tests/frontend/test_no_untrusted_images.py __________
tests/frontend/test_no_untrusted_images.py:31: in <module>
    import httpx
E   ModuleNotFoundError: No module named 'httpx'
=========================== short test summary info ============================
ERROR tests/api - ModuleNotFoundError: No module named 'httpx'
ERROR tests/frontend/test_no_untrusted_images.py
!!!!!!!!!!!!!!!!!!! Interrupted: 2 errors during collection !!!!!!!!!!!!!!!!!!!!
1 skipped, 1 deselected, 2 errors in 5.80s
make: *** [Makefile:36: test] Error 2
```

With both directories ignored, two more tests failed, because pyarrow (needed for
`DataFrame.to_parquet` in tests) arrives only through `streamlit`:

```
FAILED tests/sandbox/test_runner_branches.py::test_load_dataframes_parquet_and_sqlite
FAILED tests/sandbox/test_python_security.py::test_native_io_inside_work_dir_still_works
E   ImportError: Unable to find a usable engine; tried using: 'pyarrow', 'fastparquet'.
```

**Fix.** `httpx` and `pyarrow` are test-only for the core suite, so they are now in the `dev`
dependency group. The agent itself reads Parquet through DuckDB and does not need pyarrow.
The five API test modules that need FastAPI start with
`pytest.importorskip("fastapi", reason="needs the 'api' extra: uv sync --extra api")`.
`analyst_agent.api` now imports `create_app` lazily, so `test_store.py` and
`test_agent_adapter.py`, which do not need FastAPI, still run without the extra. A
module-level skip inside `conftest.py` is not an option: pytest reports it as an error.

After (clone A, `pytest -q -rs`):

```
SKIPPED [1] tests/api/test_app.py:9: needs the 'api' extra: uv sync --extra api
SKIPPED [1] tests/api/test_approval_api.py:12: needs the 'api' extra: uv sync --extra api
SKIPPED [1] tests/api/test_integration_core.py:8: needs the 'api' extra: uv sync --extra api
SKIPPED [1] tests/api/test_observability.py:11: needs the 'api' extra: uv sync --extra api
SKIPPED [1] tests/api/test_samples.py:10: needs the 'api' extra: uv sync --extra api
SKIPPED [1] tests/test_mcp_server.py:13: could not import 'mcp': No module named 'mcp'
SKIPPED [1] tests/test_extras.py:64: needs the 'mcp' extra: uv sync --extra mcp
SKIPPED [1] tests/test_extras.py:93: needs the 'api' extra
958 passed, 8 skipped, 1 deselected in 395.38s (0:06:35)
```

`tests/test_release_hygiene.py::test_httpx_is_declared_once` asserted a single `"httpx`
string in `pyproject.toml`. It is now `test_httpx_is_declared_once_per_role`: once in the
`frontend` extra (runtime), once in `dev` (tests), and never in the core dependencies.

### F2. `analyst-agent-mcp` without the `mcp` extra: a chained traceback (fixed)

Clone A, before (exit 1):

```
Traceback (most recent call last):
  File ".../src/analyst_agent/mcp_server.py", line 26, in <module>
    from mcp import types
ModuleNotFoundError: No module named 'mcp'
The above exception was the direct cause of the following exception:
Traceback (most recent call last):
  File ".../.venv/bin/analyst-agent-mcp", line 4, in <module>
    from analyst_agent.mcp_server import main
  File ".../src/analyst_agent/mcp_server.py", line 30, in <module>
    raise ImportError(
ImportError: analyst_agent.mcp_server needs the 'mcp' extra: pip install 'analyst-agent[mcp]' (or `uv sync --extra mcp`).
```

`python -m analyst_agent.mcp_server --help` printed the same traceback.

**Fix.** A new `analyst_agent.extras.MissingExtraError` (an `ImportError` subclass) carries the
extra's name and one message format. The console script now points at
`analyst_agent.extras:mcp_main`, which catches the error. `mcp_server.py` does the same when it
is run as `__main__`. Both now print the following and exit 2:

```
analyst-agent-mcp: the MCP server needs the optional 'mcp' extra: uv sync --extra mcp (or pip install 'analyst-agent[mcp]')
```

Library imports still raise `MissingExtraError`, so `importorskip` and callers that catch
`ImportError` behave as before.

### F3. Embedding backend without `sentence-transformers`: a logged traceback (fixed)

This is not one of the seven commands, but it is the same class of bug, found while checking
"every CLI". `AGENT_SEMANTIC_LAYER_ENABLED=true AGENT_SEMANTIC_BACKEND=embedding analyst-agent
ask --dry-run ...` completed, but first logged about 30 lines to stderr:

```
2026-11-05 03:46:39,512 ERROR   analyst_agent.core.loop: semantic layer retrieval failed
Traceback (most recent call last):
  File ".../src/analyst_agent/semantic/embeddings.py", line 48, in load_sentence_transformer
    from sentence_transformers import SentenceTransformer
ModuleNotFoundError: No module named 'sentence_transformers'
The above exception was the direct cause of the following exception:
Traceback (most recent call last):
  File ".../src/analyst_agent/core/loop.py", line 324, in _retrieve_metrics
  ...
```

This also hits clone B, which skips the `embeddings` extra by design. **Fix.** The loop catches
`MissingExtraError` separately and logs one warning. As before, it records the error on the
state and continues without metric references:

```
WARNING analyst_agent.core.loop: semantic layer off for this run: the embedding backend needs the optional 'embeddings' extra: uv sync --extra embeddings (or pip install 'analyst-agent[embeddings]') [run_id=run_…]
```

Any other retrieval failure still logs the full exception.

### F4. `AGENT_LANGFUSE_ENABLED=true` without `langfuse`: a logged traceback (fixed)

Before (clone A, `ask --dry-run`):

```
2026-11-05 03:46:43,271 ERROR   analyst_agent.core.langfuse_export: AGENT_LANGFUSE_ENABLED is set but Langfuse could not be initialised
Traceback (most recent call last):
  File ".../src/analyst_agent/core/langfuse_export.py", line 60, in make_langfuse_client
    from langfuse import Langfuse  # optional dependency
ModuleNotFoundError: No module named 'langfuse'
```

After: one line. Real initialisation errors, such as a bad host, still log the exception.

```
WARNING analyst_agent.core.langfuse_export: AGENT_LANGFUSE_ENABLED is set, but Langfuse tracing needs the optional 'api' extra: uv sync --extra api (or pip install 'analyst-agent[api]'); Langfuse tracing is off for this run
```

### F5. Preflight FAILs on `[embeddings]` in every documented setup (fixed)

`make install`, the README and CI all use `--no-extra embeddings`, because that extra pulls in
torch. So `scripts/preflight.sh --dry-run` failed in clones B and C right after a correct
install:

```
  [FAIL] extra [embeddings]                            missing sentence-transformers: uv sync --locked --all-extras
...
PRE-FLIGHT FAILED: 1 check(s): extra [embeddings]
```

**Fix.** A missing `embeddings` extra is now a `WARN` unless embedding backends were asked
for: `--embeddings` (new flag), or `AGENT_SEMANTIC_BACKEND=embedding|hybrid` in the
environment or `.env`. The runbook's real pre-flight (TL;DR and step 1) now passes
`--embeddings`, because experiment 3 needs it. Its troubleshooting row now says what a missing
extra costs. After (clone B):

```
  [WARN] extra [embeddings]                            missing sentence-transformers: fine unless you run the semantic experiment's embedding / hybrid arms (then: uv sync --locked --all-extras, and pass --embeddings to make this a failure)
...
pre-flight passed (16 checks, 2 warning(s)); plan ≈ $142.58
```

With `--embeddings` it fails as before:
`PRE-FLIGHT FAILED: 1 check(s): extra [embeddings]`.

### F6. Preflight `--dry-run` claimed a real key and a real API call (fixed)

The output was confusing. In a shell with **no** `ANTHROPIC_API_KEY`, `--dry-run` printed:

```
  [ok  ] API key set                                   ANTHROPIC_API_KEY is set
  [ok  ] API key valid                                 Messages call to claude-haiku-5-5 succeeded
  [WARN] Message Batches API                           not available in this client: eval run --batch falls back to direct judge calls
```

The key was a placeholder that `--dry-run` injects, the "Messages call" went to the FakeLLM,
and the Batches warning describes the FakeLLM, not the real client. After:

```
  [ok  ] API key set                                   not checked: --dry-run uses a placeholder key
  [ok  ] API key valid                                 dry run (scripted FakeLLM): Messages call to claude-haiku-5-5 succeeded
  [WARN] Message Batches API                           dry run (scripted FakeLLM): not available in this client: eval run --batch falls back to direct judge calls
```

### F7. Flaky `test_runner_maps_engine_exceptions` under load (fixed)

This failed once in clone B, while three clones ran their suites at once. It passed 5 out of 5
times on its own:

```
FAILED tests/sandbox/test_runner_branches.py::test_runner_maps_engine_exceptions
E   AssertionError: QueryError: Connection Error: Connection already closed!
E   assert (not False and 'QueryError' == 'Timeout'
```

**Cause.** To simulate the watchdog firing, the test called `function()` on *every* live
`threading.Timer`. Under load, a cancelled watchdog from an earlier test can still be alive.
Its `cur.interrupt()` targets a closed connection and raises inside the simulated `execute`.
This is a test bug, not a sandbox bug: each production timer interrupts only its own cursor.
Reproduced deterministically by starting a stale Timer on a closed connection before the test.
**Fix.** The test fires only the timers created during its own call. It passes with the stale
Timer present.

## Not fixed / notes

| ID | Observation | Why not fixed |
|---|---|---|
| O1 | In clone A, preflight still FAILs `[api]`, `[frontend]` and `[mcp]` (`PRE-FLIGHT FAILED: 3 check(s): extra [api], extra [frontend], extra [mcp]`). | The pre-flight gates the paid benchmark run, whose documented setup is `uv sync --locked --all-extras`. Its message already names the fix. The request was specifically to soften the `embeddings` check. Whether the other extras should become warnings is the owner's call. |
| O2 | `uv run python -m analyst_agent.api.app` without the `api` extra prints a `ModuleNotFoundError: No module named 'fastapi'` traceback. | Not a supported entry point. The server is started with `uvicorn analyst_agent.api.app:app` (Dockerfile), and uvicorn is itself in the `api` extra, so without the extra there is no command to run. |
| O3 | Every `uv` command printed `warning: The UV_NATIVE_TLS environment variable is deprecated … Use UV_SYSTEM_CERTS instead.` | Comes from this session's environment, not the project. A reviewer will not see it. |
| O4 | The `ask --dry-run` and eval oracle output is identical in A, B and C (North America, 1,290,124; 4/4 gradable tasks pass, 1 L3 skip). | Nothing to fix; recorded as confirmation that a plain install is enough for the demo path. |
| O5 | In a plain install, sandboxed user code that calls `DataFrame.to_parquet` fails, because pyarrow is not a runtime dependency. | Reading Parquet works through DuckDB. Writing Parquet from `run_python` is an edge case. Promoting pyarrow (about 50 MB) to a core dependency is a product decision. |
| O6 | `tests/test_mcp_server.py` skips with pytest's default reason (`could not import 'mcp'`) instead of naming the extra. | Cosmetic. The reason is still clear. |

## Verification

- Clone A at this branch: `uv sync`, then `make lint` clean and `pytest -q -rs` 958 passed,
  8 skipped (all listed above), 0 failed or errored.
- Clone B at this branch: **`make check` green.** Install, lint, 1027 passed with 1 slow test
  deselected, coverage 94% overall (floor 92), `sandbox/` 99% (floor 97), `core/loop.py` 100%
  (floor 98), and all three offline eval gates with 0 errors and 0 timeouts.
- Clone C (Python 3.12.3) at this branch: `make test` 1027 passed;
  `scripts/preflight.sh --dry-run` passed with 2 warnings.
- New tests: `tests/test_extras.py` (message format, both MCP entry points without `mcp` in a
  child interpreter, the sentence-transformers and langfuse paths), a loop test for the
  one-line semantic warning, and preflight tests for WARN versus `--embeddings`,
  `embeddings_requested` and the dry-run labels.
