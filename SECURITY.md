# Security policy

## Reporting a vulnerability

Please do **not** open a public issue for a security problem.

Use GitHub's private vulnerability reporting: on the repository page go to
**Security → Report a vulnerability**
(<https://github.com/maxj0201/data-analyst-agent/security/advisories/new>).

Include what you can of:

- the version or commit, and the kernel / container runtime if the report is about the sandbox;
- a minimal reproduction (the SQL or Python snippet, the dataset or file name, or the prompt);
- what an attacker gains: file read or write, network access, secret disclosure, denial of
  service, or a wrong answer that a crafted dataset can force.

This is a small project without a service-level agreement. Expect an acknowledgement within a
week and a status update after triage. Fixes are released with a `CHANGELOG.md` entry; reporters
are credited unless they ask not to be.

## What the sandbox does and does not guarantee

The agent runs SQL and Python **written by an LLM** that can be steered by text inside the
data it reads (cell values, column names, file names). The sandbox exists so that such code can
only read the loaded data and compute over it.

**It aims to guarantee**, on Linux with Landlock (kernel 5.13 or newer) and seccomp:

- SQL is limited to one `SELECT` / `WITH` statement (or `EXPLAIN` of one) run against a private,
  read-only copy of the data, with external file and network access disabled; the source files
  are never modified.
- Python runs in a separate process with no network (every `socket()` is refused), no
  subprocesses or `execve`, file access limited to the interpreter, its libraries and a temp
  directory, a scrubbed environment (no API keys), and CPU, memory, output and wall-clock limits.
- Only inert file types (images, CSV, JSON, text) leave the sandbox as artifacts.
- Anything from a dataset or a tool result that reaches the model is fenced as untrusted data,
  and risky `run_sql` steps can require human approval (`AGENT_APPROVAL_ENABLED`).

**It does not guarantee:**

- **The Python-level guards are not a security boundary.** The import allowlist and audit hook
  run in the same interpreter as the attacker's code and stop honest mistakes and casual
  injection, not a skilled adversary. The kernel layers are the real boundary, and they run as
  the same Unix user as the parent process.
- **Filesystem isolation needs Landlock.** On kernels without it the sandbox falls back to the
  audit hook, which native code can bypass. This is logged as
  `sandbox confinement degraded: Landlock unavailable` (once per process) and the child's stderr
  carries `[sandbox] Landlock unavailable`. If you see it, add container-level isolation.
- **No namespace-level network isolation.** Run production workers with no egress (container
  network policy, `--network none`) as defence in depth.
- **Not portable.** On macOS and Windows only rlimits and the Python guards apply.
- **Data confidentiality inside the dataset.** Code can read and print any loaded column, PII
  included. The prompts ask the model to prefer aggregates; that is a mitigation, not a control.
- **Correctness.** The sandbox guarantees safety, not that an answer is right. The critic step
  reduces wrong answers; it does not eliminate them.
- **Availability under load.** Limits apply per call; rate-limit at the API layer.
- **Upstream bugs** in DuckDB, pandas, numpy or the kernel are out of scope.

The full threat model, the layer-by-layer table, the escape fuzzer and the list of known
limitations are in [`docs/sandbox.md`](docs/sandbox.md). Reports that show a way past a layer
that `docs/sandbox.md` says is enforced are in scope; reports that restate an item under
*What is NOT protected against* are welcome as documentation fixes.

## Supported versions

Only the latest release and `main` receive fixes.
