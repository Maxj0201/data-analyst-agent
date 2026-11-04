"""Run an agent over a benchmark task set.

Each task runs under an asyncio semaphore (``concurrency``) with a per-task timeout. Results
are appended to ``runs/<timestamp>/results.jsonl`` as soon as each task finishes, so an
interrupted run keeps everything completed so far. ``meta.json`` records the configuration
and the prompt versions (:func:`analyst_agent.core.prompts.prompt_versions`).

Isolation: by default (``isolation="process"``) every task runs in its own child process
(see :mod:`analyst_agent.eval.isolation`) that is SIGKILLed, together with anything it
spawned, when the timeout expires, so a hung agent cannot keep burning tokens or CPU after it
is recorded as ``timeout``. ``isolation="inline"`` runs the agent in the runner's event loop
(sync agents in a worker thread); it allows unpicklable agents such as closures in tests,
but a timed-out sync agent then keeps running in the background.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from analyst_agent.core.prompts import prompt_versions
from analyst_agent.eval.agent import AgentFn, AgentInput, AgentOutput, call_agent
from analyst_agent.eval.isolation import AgentTimeout, check_picklable, run_in_subprocess
from analyst_agent.eval.judge import Judge
from analyst_agent.eval.schema import Task
from analyst_agent.eval.scorers import Score, score_task

Isolation = Literal["process", "inline"]


@dataclass
class RunConfig:
    agent: str = "core"
    concurrency: int = 4
    timeout_s: float = 300.0
    isolation: Isolation = "process"
    score: bool = True
    judge_model: str | None = None
    routing_enabled: bool | None = None  # None = whatever AGENT_ROUTING_ENABLED says
    semantic_layer_enabled: bool | None = None  # None = AGENT_SEMANTIC_LAYER_ENABLED
    # Approve every human-in-the-loop gate (AGENT_APPROVAL_ENABLED); False = no approver, so
    # gated steps are denied. Applies to the core/dry-run agents (see eval.agent.load_agent).
    auto_approve: bool = True
    filters: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskResult:
    task_id: str
    difficulty: str
    tags: list[str]
    question: str
    status: str  # ok | error | timeout
    passed: bool
    score: float
    score_details: dict[str, Any]
    answer: str
    key_numbers: dict[str, Any]
    trace: list[dict[str, Any]]
    cost_usd: float
    input_tokens: int
    output_tokens: int
    latency_s: float
    tool_calls: int
    critic_rounds: int
    critic_revisions: int
    agent_status: str | None = None
    error: str | None = None
    started_at: str = ""
    routing: dict[str, Any] | None = None
    cost_by_model: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _git_sha() -> str | None:
    try:
        return (
            subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            ).stdout.strip()
            or None
        )
    except Exception:
        return None


def _progress_mark(res: TaskResult) -> str:
    if res.status != "ok":
        return res.status.upper()
    if res.passed:
        return "PASS"
    if res.score_details.get("method") == "unscored":
        return "DONE"
    return "SKIP" if res.score_details.get("skipped") else "FAIL"


def new_run_dir(base: Path = Path("runs")) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    d = base / stamp
    n = 1
    while d.exists():
        n += 1
        d = base / f"{stamp}-{n}"
    d.mkdir(parents=True)
    return d


async def run_task(
    task: Task,
    agent: AgentFn,
    judge: Judge | None,
    timeout_s: float,
    score: bool = True,
    isolation: Isolation = "process",
) -> TaskResult:
    started = _now()
    inp = AgentInput(task_id=task.id, question=task.question, dataset_paths=task.dataset_paths())
    t0 = time.perf_counter()
    status, error = "ok", None
    out = AgentOutput()
    try:
        if isolation == "process":
            out = await run_in_subprocess(agent, inp, timeout_s)
        elif isolation == "inline":
            out = await asyncio.wait_for(call_agent(agent, inp), timeout=timeout_s)
        else:
            raise ValueError(f"unknown isolation {isolation!r}")
    except (TimeoutError, AgentTimeout):
        status, error = "timeout", f"exceeded {timeout_s}s"
    except Exception as e:  # agent bugs must not kill the run
        status = "error"
        error = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=5)}"
    latency = time.perf_counter() - t0
    if status == "ok" and out.error and out.status == "error":
        status, error = "error", out.error

    if status == "ok" and score:
        sc = await score_task(task, out, judge)
    elif status == "ok":
        sc = Score(False, 0.0, "unscored", {})
    else:
        sc = Score(False, 0.0, "not_run", {"reason": status})

    return TaskResult(
        task_id=task.id,
        difficulty=task.difficulty,
        tags=task.tags,
        question=task.question,
        status=status,
        passed=sc.passed,
        score=sc.score,
        score_details={"method": sc.method, **sc.details},
        answer=out.answer,
        key_numbers=out.key_numbers,
        trace=out.trace,
        cost_usd=out.cost_usd,
        input_tokens=out.input_tokens,
        output_tokens=out.output_tokens,
        latency_s=round(latency, 3),
        tool_calls=int(out.tool_calls or 0),
        critic_rounds=out.critic_rounds,
        critic_revisions=out.critic_revisions,
        agent_status=out.status,
        error=error,
        started_at=started,
        routing=out.routing,
        cost_by_model=out.cost_by_model,
    )


async def run_tasks(
    tasks: list[Task],
    agent: AgentFn,
    *,
    judge: Judge | None = None,
    config: RunConfig | None = None,
    run_dir: Path | None = None,
    progress: bool = True,
) -> tuple[Path, list[TaskResult]]:
    """Run ``tasks`` concurrently; return the run directory and results in task order."""
    config = config or RunConfig()
    if config.isolation == "process":
        check_picklable(agent)  # fail fast, before any run directory is written
    run_dir = run_dir or new_run_dir()
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "started_at": _now(),
        "git_sha": _git_sha(),
        "n_tasks": len(tasks),
        "task_ids": [t.id for t in tasks],
        "config": asdict(config),
        "prompts": prompt_versions(),
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    results_path = run_dir / "results.jsonl"
    sem = asyncio.Semaphore(max(1, config.concurrency))
    lock = asyncio.Lock()
    done = 0

    async def one(task: Task) -> TaskResult:
        nonlocal done
        async with sem:
            res = await run_task(
                task, agent, judge, config.timeout_s, config.score, config.isolation
            )
        async with lock:
            with open(results_path, "a") as f:
                f.write(json.dumps(res.to_dict(), default=str) + "\n")
            done += 1
            if progress:
                mark = _progress_mark(res)
                print(
                    f"[{done}/{len(tasks)}] {mark:7} {task.id} "
                    f"({res.latency_s:.1f}s, ${res.cost_usd:.4f})",
                    flush=True,
                )
        return res

    results = await asyncio.gather(*(one(t) for t in tasks))
    meta["finished_at"] = _now()
    judge_cost = getattr(judge, "cost_summary", None)
    if callable(judge_cost):
        meta["judge_cost"] = judge_cost()
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    return run_dir, list(results)


def load_results(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "results.jsonl" if run_dir.is_dir() else run_dir
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]
