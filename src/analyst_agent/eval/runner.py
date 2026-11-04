"""Run an agent over a benchmark task set.

Each task runs under an asyncio semaphore (``concurrency``) with a per-task timeout. Results
are appended to ``runs/<timestamp>/results.jsonl`` as soon as each task finishes, so an
interrupted run keeps everything completed so far. ``meta.json`` records the configuration.

Note: a sync agent runs in a worker thread; on timeout the task is recorded as ``timeout``
but the thread itself cannot be killed and finishes in the background.
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
from typing import Any

from analyst_agent.eval.agent import AgentFn, AgentInput, AgentOutput, call_agent
from analyst_agent.eval.judge import Judge
from analyst_agent.eval.schema import Task
from analyst_agent.eval.scorers import Score, score_task


@dataclass
class RunConfig:
    agent: str = "core"
    concurrency: int = 4
    timeout_s: float = 300.0
    score: bool = True
    judge_model: str | None = None
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
    task: Task, agent: AgentFn, judge: Judge | None, timeout_s: float, score: bool = True
) -> TaskResult:
    started = _now()
    inp = AgentInput(task_id=task.id, question=task.question, dataset_paths=task.dataset_paths())
    t0 = time.perf_counter()
    status, error = "ok", None
    out = AgentOutput()
    try:
        out = await asyncio.wait_for(call_agent(agent, inp), timeout=timeout_s)
    except TimeoutError:
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
    run_dir = run_dir or new_run_dir()
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "started_at": _now(),
        "git_sha": _git_sha(),
        "n_tasks": len(tasks),
        "task_ids": [t.id for t in tasks],
        "config": asdict(config),
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    results_path = run_dir / "results.jsonl"
    sem = asyncio.Semaphore(max(1, config.concurrency))
    lock = asyncio.Lock()
    done = 0

    async def one(task: Task) -> TaskResult:
        nonlocal done
        async with sem:
            res = await run_task(task, agent, judge, config.timeout_s, config.score)
        async with lock:
            with open(results_path, "a") as f:
                f.write(json.dumps(res.to_dict(), default=str) + "\n")
            done += 1
            if progress:
                mark = (
                    "PASS" if res.passed else res.status.upper() if res.status != "ok" else "FAIL"
                )
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
