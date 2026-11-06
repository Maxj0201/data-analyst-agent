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

Observability: with ``AGENT_LANGFUSE_ENABLED`` each task gets a Langfuse trace id up front; the
core agents record their span tree under it (in the child process) and, once the task is
scored, the runner attaches ``accuracy`` / ``passed`` / ``judge_score`` / ``cost_usd`` /
``eval_status`` scores to that trace (:func:`analyst_agent.core.langfuse_export.eval_scores`),
so Langfuse can filter traces by pass/fail. Timed-out tasks still get their scores (on a trace
with no spans). With ``AGENT_TRACE_STORE_ENABLED`` the span trees are saved as
``<run_dir>/traces/<run_id>.json`` for ``analyst-agent trace show``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
import traceback
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from analyst_agent.core.config import Settings
from analyst_agent.core.langfuse_export import (
    eval_scores,
    make_langfuse_client,
    new_trace_id,
    push_scores,
)
from analyst_agent.core.logs import log_context
from analyst_agent.core.prompts import prompt_versions
from analyst_agent.core.tracing import Span, save_trace
from analyst_agent.eval.agent import (
    AgentFn,
    AgentInput,
    AgentOutput,
    SpendMeter,
    call_agent,
    metering,
)
from analyst_agent.eval.isolation import AgentTimeout, check_picklable, run_in_subprocess
from analyst_agent.eval.judge import BatchJudge, Judge
from analyst_agent.eval.schema import DEFAULT_BENCHMARK_ROOT, Task, load_tasks
from analyst_agent.eval.scorers import Score, judge_prompt_versions, score_task

Isolation = Literal["process", "inline"]

log = logging.getLogger(__name__)


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
    # None = AGENT_LANGFUSE_ENABLED / AGENT_TRACE_STORE_ENABLED
    langfuse_enabled: bool | None = None
    save_traces: bool | None = None
    # Judge verdicts sent as one Message Batch (eval run --batch); recorded for meta.json.
    batch_judge: bool = False
    # Stop starting new tasks once agent + judge spend for this run reaches this many USD
    # (estimated with core.llm's price table). Tasks already running finish, so a run can
    # overshoot by up to ``concurrency`` tasks. None = no cap.
    budget_usd: float | None = None
    # Ask the L3 rubric judge this many times per task and score the mean (judge variance;
    # eval run --judge-samples). The spread is recorded in score_details["judge_samples"].
    judge_samples: int = 1
    # L3 rubric judge mode: True = result figures must be in key_numbers, False = content only
    # (eval run --lenient-judge). Scores of the two modes are not comparable (eval/report.py).
    judge_strict_numbers: bool = True


@dataclass
class TaskResult:
    task_id: str
    difficulty: str
    tags: list[str]
    question: str
    status: str  # ok | error | timeout | over_budget (not started: --budget-usd reached)
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
    run_id: str | None = None
    trace_id: str | None = None  # Langfuse trace (AGENT_LANGFUSE_ENABLED)
    trace_path: str | None = None  # saved span tree (AGENT_TRACE_STORE_ENABLED)
    # Result tables the agent returned: kept so `eval run --resume` can re-grade a row without
    # re-running the agent (ADV tasks with expected_rows match against them).
    tables: list[list[dict[str, Any]]] = field(default_factory=list)
    spans: dict[str, Any] | None = field(default=None, repr=False)  # not written to jsonl

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("spans", None)
        return d


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
    if res.status == "over_budget":
        return "BUDGET"
    if res.status != "ok":
        return res.status.upper()
    if res.passed:
        return "PASS"
    if res.score_details.get("method") == "unscored":
        return "DONE"
    return "SKIP" if res.score_details.get("skipped") else "FAIL"


def _judge_spend(judge: Judge | None) -> float:
    """USD the judge has spent so far (0 when it does not track cost)."""
    cost = getattr(judge, "cost", None)
    usd = getattr(cost, "usd", cost)
    return float(usd) if isinstance(usd, int | float) else 0.0


def over_budget_result(task: Task, budget_usd: float) -> TaskResult:
    """Placeholder row for a task never started because the run hit ``--budget-usd``."""
    return TaskResult(
        task_id=task.id,
        difficulty=task.difficulty,
        tags=task.tags,
        question=task.question,
        status="over_budget",
        passed=False,
        score=0.0,
        score_details={"method": "not_run", "reason": "over_budget", "skipped": True},
        answer="",
        key_numbers={},
        trace=[],
        cost_usd=0.0,
        input_tokens=0,
        output_tokens=0,
        latency_s=0.0,
        tool_calls=0,
        critic_rounds=0,
        critic_revisions=0,
        error=f"run budget of ${budget_usd:.2f} reached before this task started",
        started_at=_now(),
    )


def new_run_dir(base: Path = Path("runs")) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    d = base / stamp
    n = 1
    while d.exists():
        n += 1
        d = base / f"{stamp}-{n}"
    d.mkdir(parents=True)
    return d


@dataclass
class _Execution:
    """The agent half of a task: what it returned and how the call ended."""

    out: AgentOutput
    status: str
    error: str | None
    latency_s: float
    started_at: str
    trace_id: str | None = None


async def _execute(
    task: Task,
    agent: AgentFn,
    timeout_s: float,
    isolation: Isolation = "process",
    trace_id: str | None = None,
    trace_metadata: dict[str, Any] | None = None,
) -> _Execution:
    started = _now()
    inp = AgentInput(
        task_id=task.id,
        question=task.question,
        dataset_paths=task.dataset_paths(),
        trace_id=trace_id,
        trace_metadata=dict(trace_metadata or {}),
    )
    t0 = time.perf_counter()
    status, error = "ok", None
    out = AgentOutput()
    meter = SpendMeter()  # what the agent reported spending, even if it never returns
    try:
        if isolation == "process":
            out = await run_in_subprocess(agent, inp, timeout_s, spend=meter)
        elif isolation == "inline":
            with metering(meter):
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
    # A timed-out or crashed task returns no output, but its LLM calls were still paid for:
    # count what it reported so --budget-usd sees it (review-2 M2). The row's cost_usd and
    # run_tasks' spend both read ``out.cost_usd``, so a resumed run carries this spend over.
    if not out.cost_usd and meter.usd:
        out.cost_usd = meter.usd
    return _Execution(out, status, error, latency, started, trace_id)


async def _score(
    task: Task,
    ex: _Execution,
    judge: Judge | None,
    score: bool,
    judge_samples: int = 1,
    strict_numbers: bool = True,
) -> Score:
    if ex.status == "ok" and score:
        return await score_task(
            task, ex.out, judge, judge_samples=judge_samples, strict_numbers=strict_numbers
        )
    if ex.status == "ok":
        return Score(False, 0.0, "unscored", {})
    return Score(False, 0.0, "not_run", {"reason": ex.status})


def _result(task: Task, ex: _Execution, sc: Score) -> TaskResult:
    out = ex.out
    return TaskResult(
        task_id=task.id,
        difficulty=task.difficulty,
        tags=task.tags,
        question=task.question,
        status=ex.status,
        passed=sc.passed,
        score=sc.score,
        score_details={"method": sc.method, **sc.details},
        answer=out.answer,
        key_numbers=out.key_numbers,
        trace=out.trace,
        cost_usd=round(out.cost_usd or 0.0, 6),
        input_tokens=out.input_tokens,
        output_tokens=out.output_tokens,
        latency_s=round(ex.latency_s, 3),
        tool_calls=int(out.tool_calls or 0),
        critic_rounds=out.critic_rounds,
        critic_revisions=out.critic_revisions,
        agent_status=out.status,
        error=ex.error,
        started_at=ex.started_at,
        routing=out.routing,
        cost_by_model=out.cost_by_model,
        run_id=out.run_id,
        trace_id=out.trace_id or ex.trace_id,
        tables=out.tables,
        spans=out.spans,
    )


async def run_task(
    task: Task,
    agent: AgentFn,
    judge: Judge | None,
    timeout_s: float,
    score: bool = True,
    isolation: Isolation = "process",
    trace_id: str | None = None,
    trace_metadata: dict[str, Any] | None = None,
) -> TaskResult:
    ex = await _execute(task, agent, timeout_s, isolation, trace_id, trace_metadata)
    return _result(task, ex, await _score(task, ex, judge, score))


# --------------------------------------------------------------------------------------------
# Resume: which tasks of an earlier (interrupted) run still need work
# --------------------------------------------------------------------------------------------

JUDGED_LEVELS = frozenset({"L3", "ADV"})
PENDING_JUDGE = "pending_judge"


def judge_incomplete(row: dict[str, Any]) -> bool:
    """Whether a finished agent run still lacks a real judge verdict.

    True for a row written while its verdict was pending (``--batch``, interrupted before the
    batch came back), and for L3 / ADV rows whose judge was unavailable or failed.
    """
    if row.get("status") != "ok":
        return False
    d = row.get("score_details") or {}
    method = d.get("method")
    if method == PENDING_JUDGE:
        return True
    if method == "rubric_judge":
        return "points" not in d and bool(d.get("skipped") or "error" in d)
    if method == "adversarial":
        j = d.get("judge") or {}
        return bool(j.get("skipped") or "error" in j)
    return False


ResumeAction = Literal["done", "rescore", "run"]


def resume_plan(
    rows: list[dict[str, Any]],
    task_ids: list[str],
    *,
    retry_errors: bool = False,
    rescore: bool = True,
) -> dict[str, ResumeAction]:
    """Decide, per task id, what a resumed run does with it.

    * ``run``: no row yet (never finished: crash, kill), ``over_budget`` (never started), and
      ``error`` / ``timeout`` rows when ``retry_errors`` (otherwise those are real outcomes and
      stay as they are).
    * ``rescore``: the agent finished but the judge verdict is missing (:func:`judge_incomplete`);
      the saved answer is graded again without re-running (and re-paying) the agent. Only when
      ``rescore`` (a judge is available); otherwise such rows are ``done``.
    * ``done``: already scored; skipped.
    """
    last = {r["task_id"]: r for r in rows}
    plan: dict[str, ResumeAction] = {}
    for tid in task_ids:
        row = last.get(tid)
        if row is None or row.get("status") == "over_budget":
            plan[tid] = "run"
        elif row.get("status") in ("error", "timeout"):
            plan[tid] = "run" if retry_errors else "done"
        elif rescore and judge_incomplete(row):
            plan[tid] = "rescore"
        else:
            plan[tid] = "done"
    return plan


def _row_result(row: dict[str, Any]) -> TaskResult:
    known = {f.name for f in fields(TaskResult)}
    return TaskResult(**{k: v for k, v in row.items() if k in known})


def _row_execution(row: dict[str, Any]) -> _Execution:
    out = AgentOutput(
        answer=row.get("answer", ""),
        key_numbers=row.get("key_numbers") or {},
        tables=row.get("tables") or [],
        trace=row.get("trace") or [],
        cost_usd=row.get("cost_usd", 0.0),
        input_tokens=row.get("input_tokens", 0),
        output_tokens=row.get("output_tokens", 0),
        tool_calls=row.get("tool_calls"),
        critic_rounds=row.get("critic_rounds", 0),
        critic_revisions=row.get("critic_revisions", 0),
        status=row.get("agent_status") or "ok",
        routing=row.get("routing"),
        cost_by_model=row.get("cost_by_model") or {},
        run_id=row.get("run_id"),
        trace_id=row.get("trace_id"),
    )
    return _Execution(
        out, "ok", None, row.get("latency_s", 0.0), row.get("started_at", ""), row.get("trace_id")
    )


# Settings that change what an agent run measures; recorded in meta.json and compared on resume.
SETTINGS_SNAPSHOT = (
    "model",
    "critic_model",
    "effort",
    "max_tokens",
    "max_turns",
    "token_budget",
    "max_critic_rounds",
    "routing_enabled",
    "router_model",
    "model_l1",
    "model_l2",
    "model_l3",
    "semantic_layer_enabled",
    "semantic_backend",
    "semantic_top_k",
    "semantic_hybrid_weight",
    "structured_report_enabled",
    "memory_enabled",
    "approval_enabled",
    "judge_model",
    "judge_effort",
    "judge_strict_numbers",
)


def settings_snapshot(settings: Settings) -> dict[str, Any]:
    return {k: getattr(settings, k) for k in SETTINGS_SNAPSHOT}


class ResumeError(ValueError):
    """The run directory cannot be resumed as asked (missing files, different settings)."""


def settings_mismatch(recorded: dict[str, Any] | None, current: dict[str, Any]) -> list[str]:
    """``AGENT_*`` settings that differ between an earlier run and now (``[]`` = same)."""
    if not recorded:
        return []
    return [
        f"AGENT_{k.upper()}: run used {recorded.get(k)!r}, now {current.get(k)!r}"
        for k in SETTINGS_SNAPSHOT
        if k in recorded and recorded.get(k) != current.get(k)
    ]


def _publish_trace(
    res: TaskResult, task: Task, run_dir: Path, langfuse: Any | None, save: bool
) -> None:
    """Save the span tree and push the scores to Langfuse. Never raises."""
    if save and res.spans:
        try:
            root = Span.from_dict(res.spans)
            path = save_trace(run_dir / "traces", root, name=res.run_id or res.task_id)
            res.trace_path = str(path)
        except Exception:  # noqa: BLE001 - observability must not fail an eval run
            log.exception("could not save trace for task %s", res.task_id)
    if langfuse is not None and res.trace_id:
        scores = eval_scores(
            status=res.status,
            passed=res.passed,
            score=res.score,
            score_details=res.score_details,
            cost_usd=res.cost_usd,
        )
        push_scores(
            langfuse,
            res.trace_id,
            scores,
            comment=f"{task.id} ({task.difficulty}): {_progress_mark(res)}",
            metadata={
                "task_id": task.id,
                "difficulty": task.difficulty,
                "tags": task.tags,
                "eval_run": run_dir.name,
                "run_id": res.run_id,
            },
        )


async def run_tasks(
    tasks: list[Task],
    agent: AgentFn,
    *,
    judge: Judge | None = None,
    config: RunConfig | None = None,
    run_dir: Path | None = None,
    progress: bool = True,
    langfuse: Any | None = None,
    resume: bool = False,
    retry_errors: bool = False,
) -> tuple[Path, list[TaskResult]]:
    """Run ``tasks`` concurrently; return the run directory and results in task order.

    ``langfuse`` is the client scores are pushed with; by default one is created when
    Langfuse is enabled (``config.langfuse_enabled``, else ``AGENT_LANGFUSE_ENABLED``).

    ``judge`` may be a :class:`~analyst_agent.eval.judge.BatchJudge`: agents then run under
    the semaphore as usual, every L3 / ADV row is first written with a ``pending_judge``
    score, and the queued verdicts are sent as one Message Batch once every remaining task is
    waiting on the judge. Judge spend is then known only when the batch returns, so the
    ``budget_usd`` check sees it late (the judge is a few percent of a run's cost).

    ``resume=True`` continues the run in ``run_dir`` (``meta.json`` + ``results.jsonl``):
    see :func:`resume_plan`. New rows are appended; :func:`load_results` keeps the last row
    per task. Spend already recorded counts towards ``budget_usd``. Raises
    :class:`ResumeError` when the task list or the recorded ``AGENT_*`` settings differ.
    """
    config = config or RunConfig()
    settings = Settings()
    lf_enabled = (
        config.langfuse_enabled
        if config.langfuse_enabled is not None
        else settings.langfuse_enabled
    )
    save_traces = (
        config.save_traces if config.save_traces is not None else (settings.trace_store_enabled)
    )
    if config.isolation == "process":
        check_picklable(agent)  # fail fast, before any run directory is written

    snapshot = settings_snapshot(settings)
    results_path: Path
    plan: dict[str, ResumeAction] = {t.id: "run" for t in tasks}
    prev_rows: dict[str, dict[str, Any]] = {}
    prior_spend = 0.0
    if resume:
        if run_dir is None or not (run_dir / "meta.json").is_file():
            raise ResumeError(f"{run_dir} is not an eval run directory (no meta.json)")
        meta = json.loads((run_dir / "meta.json").read_text())
        if meta.get("task_ids") and set(meta["task_ids"]) != {t.id for t in tasks}:
            raise ResumeError("the task list differs from the run being resumed")
        diff = settings_mismatch(meta.get("settings"), json.loads(json.dumps(snapshot)))
        if diff:
            raise ResumeError(
                "AGENT_* settings differ from the run being resumed (export the same "
                "environment, e.g. from run_experiments.sh --print-commands): " + "; ".join(diff)
            )
        results_path = run_dir / "results.jsonl"
        rows = load_results(run_dir) if results_path.is_file() else []
        prev_rows = {r["task_id"]: r for r in rows}
        plan = resume_plan(
            rows, [t.id for t in tasks], retry_errors=retry_errors, rescore=judge is not None
        )
        recorded = meta.get("spend_usd")
        prior_spend = (
            float(recorded)
            if recorded is not None
            else sum(float(r.get("cost_usd") or 0.0) for r in rows)
        )
        counts = {a: sum(v == a for v in plan.values()) for a in ("done", "rescore", "run")}
        meta.setdefault("resumes", []).append(
            {
                "at": _now(),
                "git_sha": _git_sha(),
                "prior_spend_usd": round(prior_spend, 6),
                "retry_errors": retry_errors,
                **counts,
            }
        )
        meta.pop("finished_at", None)
        meta["config"] = asdict(config)
        if progress:
            print(
                f"resuming {run_dir}: {counts['done']} done, {counts['rescore']} to re-score, "
                f"{counts['run']} to run (spent so far ${prior_spend:.2f})",
                flush=True,
            )
    else:
        run_dir = run_dir or new_run_dir()
        run_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "started_at": _now(),
            "git_sha": _git_sha(),
            "n_tasks": len(tasks),
            "task_ids": [t.id for t in tasks],
            "config": asdict(config),
            "settings": snapshot,
            "prompts": {**prompt_versions(), **judge_prompt_versions()},
        }
        results_path = run_dir / "results.jsonl"
    assert run_dir is not None
    _write_meta(run_dir, meta)

    if lf_enabled and langfuse is None:
        langfuse = make_langfuse_client()
    if not lf_enabled:
        langfuse = None
    sem = asyncio.Semaphore(max(1, config.concurrency))
    lock = asyncio.Lock()
    done = 0
    agent_spend = 0.0
    budget = config.budget_usd
    work = [t for t in tasks if plan[t.id] != "done"]
    total = len(work)

    # --batch: verdicts are queued and sent together once every unfinished task waits on them.
    batch_judge = judge if isinstance(judge, BatchJudge) else None
    unfinished = total
    waiting = 0  # tasks whose scorer is waiting on the batch judge
    flushing = False
    # Set when the run is being cancelled (Ctrl-C): no new batch may be sent then, since nobody
    # would wait for its verdicts and their (paid) spend would never be recorded.
    stopping = False
    flushes: set[asyncio.Task[None]] = set()

    def maybe_flush() -> None:
        nonlocal flushing
        if batch_judge is None or flushing or stopping or not batch_judge.pending:
            return
        if waiting >= unfinished:
            flushing = True
            t = asyncio.ensure_future(do_flush())
            flushes.add(t)
            t.add_done_callback(flushes.discard)

    async def do_flush() -> None:
        nonlocal flushing
        assert batch_judge is not None
        try:
            await batch_judge.flush()
        finally:
            flushing = False
            maybe_flush()

    if batch_judge is not None:
        batch_judge.on_enqueue = maybe_flush

    def spent() -> float:
        return prior_spend + agent_spend + _judge_spend(judge)

    def write(res: TaskResult) -> None:
        with open(results_path, "a") as f:
            f.write(json.dumps(res.to_dict(), default=str) + "\n")
        # Keep the spend recorded so far in meta.json, so a run killed mid-way (Ctrl-C, OOM,
        # a lost VM) is resumed with everything it paid for, judge calls (direct or batched)
        # included. Only the spend of tasks still in flight at the kill is lost.
        meta["spend_usd"] = round(spent(), 6)
        _write_meta(run_dir, meta)

    async def one(task: Task) -> TaskResult:
        nonlocal done, agent_spend, unfinished, stopping, waiting
        action = plan[task.id]
        trace_id = new_trace_id() if langfuse is not None and action == "run" else None
        new_spend = 0.0
        try:
            with log_context(task_id=task.id):
                async with sem:
                    if action == "rescore":
                        ex = _row_execution(prev_rows[task.id])
                    elif budget is not None and spent() >= budget:
                        ex = None
                    else:
                        ex = await _execute(
                            task,
                            agent,
                            config.timeout_s,
                            config.isolation,
                            trace_id=trace_id,
                            trace_metadata={
                                "eval_run": run_dir.name,
                                "difficulty": task.difficulty,
                            },
                        )
                        new_spend = ex.out.cost_usd
                    if ex is not None and batch_judge is None:
                        sc = await _score(
                            task,
                            ex,
                            judge,
                            config.score,
                            config.judge_samples,
                            config.judge_strict_numbers,
                        )
                        res = _result(task, ex, sc)
                if ex is None:
                    res = over_budget_result(task, budget or 0.0)
                elif batch_judge is not None:
                    deferred = (
                        action == "run"
                        and ex.status == "ok"
                        and config.score
                        and task.difficulty in JUDGED_LEVELS
                    )
                    if deferred:  # keep the (paid-for) agent output if the batch never returns
                        sc = Score(False, 0.0, PENDING_JUDGE, {"skipped": True})
                        pending = _result(task, ex, sc)
                        async with lock:
                            agent_spend += new_spend
                            new_spend = 0.0
                            write(pending)
                    waiting += 1
                    try:
                        sc = await _score(
                            task,
                            ex,
                            judge,
                            config.score,
                            config.judge_samples,
                            config.judge_strict_numbers,
                        )
                    finally:
                        waiting -= 1
                    res = _result(task, ex, sc)
                if res.status != "over_budget" and action == "run":
                    _publish_trace(res, task, run_dir, langfuse, save_traces)
            async with lock:
                agent_spend += new_spend
                write(res)
                done += 1
                if progress:
                    mark = _progress_mark(res)
                    note = " (re-scored)" if action == "rescore" else ""
                    print(
                        f"[{done}/{total}] {mark:7} {task.id} "
                        f"({res.latency_s:.1f}s, ${res.cost_usd:.4f}){note}",
                        flush=True,
                    )
            return res
        except asyncio.CancelledError:
            stopping = True
            raise
        finally:
            unfinished -= 1
            maybe_flush()

    try:
        new = await asyncio.gather(*(one(t) for t in work))
    finally:
        if batch_judge is not None:
            batch_judge.on_enqueue = None
    by_id = {r.task_id: r for r in new}
    results = [by_id[t.id] if t.id in by_id else _row_result(prev_rows[t.id]) for t in tasks]
    meta["finished_at"] = _now()
    if langfuse is not None:
        try:
            langfuse.flush()
        except Exception:  # noqa: BLE001
            log.exception("Langfuse flush failed")
    judge_cost = getattr(judge, "cost_summary", None)
    if callable(judge_cost):
        meta["judge_cost"] = judge_cost()
    if batch_judge is not None:
        meta["judge_batch"] = batch_judge.stats.to_dict()
    meta["spend_usd"] = round(spent(), 6)
    if budget is not None:
        skipped = sum(r.status == "over_budget" for r in results)
        meta["budget"] = {"usd": budget, "exhausted": skipped > 0, "tasks_not_started": skipped}
    _write_meta(run_dir, meta)
    return run_dir, results


def _write_meta(run_dir: Path, meta: dict[str, Any]) -> None:
    """Replace ``meta.json`` atomically: a kill mid-write must not leave it truncated."""
    tmp = run_dir / "meta.json.tmp"
    tmp.write_text(json.dumps(meta, indent=2, default=str))
    tmp.replace(run_dir / "meta.json")


def load_results(run_dir: Path) -> list[dict[str, Any]]:
    """Rows of ``results.jsonl``, one per task: the last row written for each task wins.

    A task can have several rows: a ``pending_judge`` row followed by its verdict (``--batch``),
    or rows appended by ``eval run --resume``. Order is that of each task's first row.
    """
    path = run_dir / "results.jsonl" if run_dir.is_dir() else run_dir
    rows: dict[str, dict[str, Any]] = {}
    with open(path) as f:
        for line in f:
            if line.strip():
                row = json.loads(line)
                rows[row.get("task_id", f"#{len(rows)}")] = row
    return list(rows.values())


class _ReplayJudge:
    """Answers an ADV behaviour prompt with the verdict a run already recorded (no API call)."""

    def __init__(self, verdict: dict[str, Any]) -> None:
        self.verdict = verdict

    async def complete(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        if "points" in schema.get("properties", {}):  # pragma: no cover - L3 is never replayed
            raise RuntimeError("rescore does not re-judge L3 rubric tasks")
        return {
            "passed": bool(self.verdict.get("passed")),
            "justification": self.verdict.get("justification", ""),
        }


def rescore_run(
    run_dir: Path, out_dir: Path | None = None, benchmarks: Path | None = None
) -> tuple[Path, list[dict[str, Any]]]:
    """Re-apply today's deterministic scorers to a finished run, without any model call.

    L1 / L2 are scored again from the stored answer, tables and key_numbers. ADV keeps the judge
    verdict the run recorded and re-applies the rule checks and the answer check. L3 (the rubric
    judge) and rows that did not finish are copied unchanged. Writes ``results.jsonl``,
    ``meta.json`` (with a ``rescored`` entry) and ``report.md`` to ``out_dir`` (default
    ``<run_dir>/rescored``, inside the run so tools that pick an arm's latest run ignore it) and
    returns it with the list of tasks whose result changed.
    """
    from analyst_agent.eval.report import report_run
    from analyst_agent.eval.scorers import score_adversarial, score_answer

    rows = load_results(run_dir)
    meta = json.loads((run_dir / "meta.json").read_text())
    root = benchmarks or DEFAULT_BENCHMARK_ROOT
    tasks = {t.id: t for t in load_tasks(root, ids=[r["task_id"] for r in rows])}
    out_dir = out_dir or run_dir / "rescored"
    out_dir.mkdir(parents=True, exist_ok=True)
    changes: list[dict[str, Any]] = []
    new_rows: list[dict[str, Any]] = []
    for row in rows:
        task = tasks.get(row["task_id"])
        new = dict(row)
        if task is not None and row.get("status") == "ok" and task.difficulty != "L3":
            out = AgentOutput(
                answer=row.get("answer") or "",
                key_numbers=row.get("key_numbers") or {},
                tables=row.get("tables") or [],
            )
            if task.difficulty == "ADV":
                stored = (row.get("score_details") or {}).get("judge") or {}
                judge = None if stored.get("skipped") or "passed" not in stored else stored
                sc = asyncio.run(
                    score_adversarial(task, out, None if judge is None else _ReplayJudge(judge))
                )
            else:
                sc = score_answer(task.expected, out)
            details = {"method": sc.method, **sc.details}  # same shape as a run writes
            new.update(passed=sc.passed, score=sc.score, score_details=details)
            if (new["passed"], new["score"]) != (row.get("passed"), row.get("score")):
                changes.append(
                    {
                        "task_id": row["task_id"],
                        "before": {"passed": row.get("passed"), "score": row.get("score")},
                        "after": {"passed": new["passed"], "score": new["score"]},
                    }
                )
        new_rows.append(new)
    with open(out_dir / "results.jsonl", "w") as f:
        for r in new_rows:
            f.write(json.dumps(r, default=str) + "\n")
    meta["rescored"] = {
        "from": str(run_dir),
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "scorer_git_sha": _git_sha(),
        "rescored": "L1, L2, ADV rules (stored judge verdicts); L3 copied",
        "changes": changes,
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2, default=str))
    (out_dir / "report.md").write_text(report_run(out_dir))
    return out_dir, changes
