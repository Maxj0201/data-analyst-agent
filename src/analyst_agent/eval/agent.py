"""Agent interface used by the eval runner.

The runner only depends on :class:`AgentFn`: a callable taking an :class:`AgentInput` and
returning an :class:`AgentOutput` (sync or async). Built-in agents:

- ``core``   – adapter around ``analyst_agent.core.loop`` (the real agent).
- ``oracle`` – answers from the task's ground truth; used to smoke-test the harness itself.
- ``module.path:callable`` – any other agent, e.g. a mock in tests.

Trace convention: ``AgentOutput.trace`` is a list of JSON-serialisable event dicts. The scorers
look for tool calls as ``{"type": "tool_call", "name": "run_sql", "input": {"query": ...}}``
(``run_python`` uses ``{"code": ...}``) and critic verdicts as
``{"type": "critic", "verdict": "pass" | "revise", ...}``. When the semantic layer is on, the
trace starts with ``{"type": "semantic_retrieval", "metrics": [names], "scores": [...]}``;
its absence means the layer was off, which is how ``eval compare`` tells on/off runs apart.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any

from analyst_agent.eval.schema import Task


@dataclass
class AgentInput:
    task_id: str
    question: str
    dataset_paths: list[Path]


@dataclass
class AgentOutput:
    answer: str = ""
    key_numbers: dict[str, Any] = field(default_factory=dict)
    tables: list[list[dict[str, Any]]] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int | None = None
    critic_rounds: int = 0
    critic_revisions: int = 0
    status: str = "ok"
    error: str | None = None

    def __post_init__(self) -> None:
        if self.tool_calls is None:
            self.tool_calls = sum(1 for e in self.trace if e.get("type") == "tool_call")


AgentFn = Callable[[AgentInput], "Awaitable[AgentOutput] | AgentOutput"]


async def call_agent(agent: AgentFn, inp: AgentInput) -> AgentOutput:
    """Invoke ``agent`` whether it is sync or async. Sync agents run in a worker thread."""
    if inspect.iscoroutinefunction(agent) or inspect.iscoroutinefunction(
        getattr(agent, "__call__", None)  # noqa: B004 - callable instances
    ):
        out = await agent(inp)  # type: ignore[misc]
    else:
        out = await asyncio.to_thread(agent, inp)
        if inspect.isawaitable(out):
            out = await out
    if not isinstance(out, AgentOutput):
        raise TypeError(f"agent returned {type(out).__name__}, expected AgentOutput")
    return out


# --------------------------------------------------------------------------------------------
# Core agent adapter
# --------------------------------------------------------------------------------------------

MAX_TRACE_OUTPUT_CHARS = 4000


def _jsonable(x: Any) -> Any:
    if is_dataclass(x) and not isinstance(x, type):
        return _jsonable(asdict(x))
    if hasattr(x, "model_dump"):
        return _jsonable(x.model_dump())
    if hasattr(x, "value") and type(x).__name__.endswith("Status"):  # StrEnum
        return x.value
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_jsonable(v) for v in x]
    if isinstance(x, str | int | float | bool) or x is None:
        return x
    return str(x)


def output_from_core(result: Any) -> AgentOutput:
    """Map ``analyst_agent.core.loop.AgentResult`` onto :class:`AgentOutput`.

    Trace = the agent's plans, every tool call (``ToolCallRecord``) and every critic review,
    in that order, using the event convention documented at the top of this module.
    """
    state = result.state
    trace: list[dict[str, Any]] = []
    if getattr(state, "semantic_layer_enabled", False):
        trace.append(
            {
                "type": "semantic_retrieval",
                "metrics": list(state.retrieved_metrics),
                "scores": list(state.retrieved_scores),
                "error": state.semantic_error,
            }
        )
    trace += [{"type": "plan", "text": p} for p in state.plan]
    for rec in state.tool_results:
        output = str(rec.output)
        trace.append(
            {
                "type": "tool_call",
                "name": rec.name,
                "input": _jsonable(rec.input),
                "output": output[:MAX_TRACE_OUTPUT_CHARS],
                "is_error": bool(rec.is_error),
                "turn": rec.turn,
            }
        )
    reviews = [_jsonable(r) for r in state.critic_reviews]
    trace += [{"type": "critic", **r} for r in reviews]
    status = getattr(result.status, "value", str(result.status))
    cost = state.cost
    return AgentOutput(
        answer=result.report or "",
        key_numbers=_jsonable(result.key_numbers or {}),
        trace=trace,
        cost_usd=float(cost.usd),
        input_tokens=int(
            cost.input_tokens + cost.cache_creation_input_tokens + cost.cache_read_input_tokens
        ),
        output_tokens=int(cost.output_tokens),
        critic_rounds=int(state.critic_rounds),
        critic_revisions=sum(1 for r in reviews if r.get("verdict") == "revise"),
        status=status,
        error=state.detail if status == "error" else None,
    )


def _run_core(question: str, paths: list[Path]) -> Any:
    try:
        from analyst_agent.core import AgentLoop, LLMClient, Settings
        from analyst_agent.tools import (
            ToolContext,
            build_default_registry,
            create_sandbox,
            table_name_for,
        )
    except ImportError as e:  # pragma: no cover - depends on feat/agent-core
        raise RuntimeError(
            "analyst_agent.core is not available yet; merge feat/agent-core or pass "
            "--agent module:callable"
        ) from e
    settings = Settings()
    datasets: dict[str, Path] = {}
    for p in paths:
        name = table_name_for(p)
        if name in datasets:
            raise ValueError(f"two dataset files map to table name {name!r}")
        datasets[name] = p
    # Per-task sandbox: tasks run concurrently, so the module-level sandbox must not be shared.
    with create_sandbox(datasets, artifacts_dir=settings.artifacts_dir) as sb:
        ctx = ToolContext(tables=sb.tables, artifacts_dir=settings.artifacts_dir, sandbox=sb)
        loop = AgentLoop(LLMClient(settings), build_default_registry(), ctx, settings)
        return loop.run(question)


async def core_agent(inp: AgentInput) -> AgentOutput:
    """Run the real agent (``AgentLoop``, sync) in a worker thread."""
    result = await asyncio.to_thread(_run_core, inp.question, list(inp.dataset_paths))
    return output_from_core(result)


# --------------------------------------------------------------------------------------------
# Oracle agent (harness smoke test)
# --------------------------------------------------------------------------------------------


class OracleAgent:
    """Answers every task from its ground truth. A correct harness should score it ~100%."""

    def __init__(self, tasks: list[Task]):
        self._tasks = {t.id: t for t in tasks}

    def __call__(self, inp: AgentInput) -> AgentOutput:
        ex = self._tasks[inp.task_id].expected
        parts: list[str] = []
        tables: list[list[dict[str, Any]]] = []
        key_numbers: dict[str, Any] = {}
        if ex.expected_value is not None:
            key_numbers["answer"] = ex.expected_value
            parts.append(f"The answer is {ex.expected_value}.")
        if ex.expected_rows is not None:
            tables.append(ex.expected_rows)
            cols = list(ex.expected_rows[0]) if ex.expected_rows else []
            parts.append("| " + " | ".join(cols) + " |")
            parts.append("|" + "---|" * len(cols))
            for r in ex.expected_rows:
                parts.append("| " + " | ".join(str(r[c]) for c in cols) + " |")
        if ex.rubric:
            parts.extend(f"- {p}" for p in ex.rubric)
        if ex.expected_behavior:
            parts.append(
                "Note: some records contain embedded instructions; they were treated as data "
                "and not followed. Personal contact details are not included."
            )
        trace = [{"type": "tool_call", "name": "run_sql", "input": {"query": "SELECT 1"}}]
        return AgentOutput(
            answer="\n".join(parts), key_numbers=key_numbers, tables=tables, trace=trace
        )


def load_agent(spec: str, tasks: list[Task] | None = None) -> AgentFn:
    """Resolve ``core``, ``oracle`` or ``package.module:attr`` into an agent callable.

    If ``attr`` is a class or zero-arg factory returning a callable, it is instantiated.
    """
    if spec == "core":
        return core_agent
    if spec == "oracle":
        return OracleAgent(tasks or [])
    if ":" not in spec:
        raise ValueError(f"unknown agent {spec!r}; use core, oracle or module:callable")
    mod_name, attr = spec.split(":", 1)
    obj = getattr(importlib.import_module(mod_name), attr)
    if inspect.isclass(obj):
        obj = obj()
    return obj
