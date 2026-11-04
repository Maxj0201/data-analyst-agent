"""Agent interface used by the eval runner.

The runner only depends on :class:`AgentFn`: a callable taking an :class:`AgentInput` and
returning an :class:`AgentOutput` (sync or async). Built-in agents:

- ``core``    – adapter around ``analyst_agent.core.loop`` (the real agent).
- ``dry-run`` – the real agent loop, tools and sandbox driven by the scripted offline
  ``FakeLLM`` (:func:`analyst_agent.core.fake_llm.dry_run_llm`): no API key, no cost. Used in
  CI to catch prompt / loop changes that break the pipeline; its answers are not analyses.
- ``oracle``  – answers from the task's ground truth; used to smoke-test the harness itself.
- ``module.path:callable`` – any other agent, e.g. a mock in tests.

Trace convention: ``AgentOutput.trace`` is a list of JSON-serialisable event dicts. The scorers
look for tool calls as ``{"type": "tool_call", "name": "run_sql", "input": {"query": ...}}``
(``run_python`` uses ``{"code": ...}``) and critic verdicts as
``{"type": "critic", "verdict": "pass" | "revise", ...}``. The core adapters prepend, in
this order and only when the feature is on:

- model routing: ``{"type": "route", "tier": ..., "model": ...}``; one
  ``{"type": "escalation", "from_tier": ..., "to_tier": ..., "trigger": ...}`` per escalation
  follows the tool calls, and each tool call is tagged with the ``model`` that issued it;
- semantic layer: ``{"type": "semantic_retrieval", "metrics": [names], "scores": [...]}``.
  Its absence means the layer was off, which is how ``eval compare`` tells on/off runs apart.

After the tool calls (and escalations) come one ``{"type": "approval", "tool": ..., "approved":
..., "decided_by": "auto" | ...}`` per human-in-the-loop gate when ``AGENT_APPROVAL_ENABLED`` is
on; eval runs auto-approve them unless ``eval run --no-auto-approve`` is given.

Tracing: the ``core`` / ``dry-run`` agents also record the run's span tree
(:class:`~analyst_agent.core.tracing.TraceRecorder`) and return it as ``AgentOutput.spans``,
with the loop's ``run_id``. When the runner passes ``AgentInput.trace_id``
(``AGENT_LANGFUSE_ENABLED``), the tree is mirrored to Langfuse under that trace id, so the
runner can attach the task's scores to it afterwards.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any

from analyst_agent.core.langfuse_export import LangfuseSink, make_langfuse_client
from analyst_agent.core.logs import log_context
from analyst_agent.core.tracing import EventedLLM, SpanSink, TraceRecorder
from analyst_agent.eval.schema import Task


@dataclass
class AgentInput:
    task_id: str
    question: str
    dataset_paths: list[Path]
    trace_id: str | None = None  # Langfuse trace id to record under (None = no Langfuse)
    trace_metadata: dict[str, Any] = field(default_factory=dict)  # e.g. eval run dir


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
    # Model routing: ``AgentState.routing_summary()`` (None when routing is off) and the
    # per-model token/cost breakdown from ``CostTracker.by_model``.
    routing: dict[str, Any] | None = None
    cost_by_model: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Tracing (core adapters only): the loop's run id, the Langfuse trace id the run was
    # recorded under, and the span tree (``Span.to_dict()``).
    run_id: str | None = None
    trace_id: str | None = None
    spans: dict[str, Any] | None = None

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


def output_from_core(result: Any, recorder: TraceRecorder | None = None) -> AgentOutput:
    """Map ``analyst_agent.core.loop.AgentResult`` onto :class:`AgentOutput`.

    Trace = the agent's plans, every tool call (``ToolCallRecord``) and every critic review,
    in that order, using the event convention documented at the top of this module.
    """
    state = result.state
    summary_fn = getattr(state, "routing_summary", None)
    routing = _jsonable(summary_fn()) if callable(summary_fn) else None
    trace: list[dict[str, Any]] = []
    if routing:
        trace.append({"type": "route", **routing["route"]})
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
        event = {
            "type": "tool_call",
            "name": rec.name,
            "input": _jsonable(rec.input),
            "output": output[:MAX_TRACE_OUTPUT_CHARS],
            "is_error": bool(rec.is_error),
            "turn": rec.turn,
        }
        if getattr(rec, "model", None):
            event["model"] = rec.model
        trace.append(event)
    if routing:
        trace += [{"type": "escalation", **e} for e in routing["escalations"]]
    for a in getattr(state, "approvals", None) or []:  # human-in-the-loop gates (if enabled)
        action, decision = _jsonable(a.action), _jsonable(a.decision)
        trace.append(
            {
                "type": "approval",
                "tool": action.get("tool"),
                "triggers": action.get("triggers"),
                "turn": action.get("turn"),
                "approved": decision.get("approved"),
                "decided_by": decision.get("decided_by"),
            }
        )
    reviews = [_jsonable(r) for r in state.critic_reviews]
    trace += [{"type": "critic", **r} for r in reviews]
    status = getattr(result.status, "value", str(result.status))
    cost = state.cost
    spans = trace_id = None
    if recorder is not None:
        root = recorder.finish(
            status=status, output=result.report, error=state.detail if status == "error" else None
        )
        spans = root.to_dict()
        trace_id = root.metadata.get("langfuse_trace_id")
    return AgentOutput(
        run_id=getattr(state, "run_id", None) or None,
        trace_id=trace_id,
        spans=spans,
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
        routing=routing,
        cost_by_model={
            str(m): _jsonable(u.to_dict()) for m, u in getattr(cost, "by_model", {}).items()
        },
    )


LLMFactory = Callable[[str, Any], Any]  # (question, sandbox) -> LLM


def make_recorder(inp: AgentInput) -> TraceRecorder:
    """Span recorder for one eval task; mirrored to Langfuse when ``inp.trace_id`` is set."""
    sinks: list[SpanSink] = []
    if inp.trace_id:
        client = make_langfuse_client()
        if client is not None:
            sinks.append(LangfuseSink(client, trace_id=inp.trace_id, root_name="analyst-eval"))
    return TraceRecorder(
        sinks=sinks,
        question=inp.question,
        metadata={"task_id": inp.task_id, **inp.trace_metadata},
    )


def _run_core(
    question: str,
    paths: list[Path],
    llm_factory: LLMFactory | None = None,
    routing_enabled: bool | None = None,
    auto_approve: bool = True,
    recorder: TraceRecorder | None = None,
) -> Any:
    try:
        from analyst_agent.core import AgentLoop, LLMClient, Settings
        from analyst_agent.core.approval import AutoApprover
        from analyst_agent.core.llm import estimate_cost_usd
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
    if routing_enabled is not None:  # eval A/B override of AGENT_ROUTING_ENABLED
        settings = settings.model_copy(update={"routing_enabled": routing_enabled})
    datasets: dict[str, Path] = {}
    for p in paths:
        name = table_name_for(p)
        if name in datasets:
            raise ValueError(f"two dataset files map to table name {name!r}")
        datasets[name] = p
    # Per-task sandbox: tasks run concurrently, so the module-level sandbox must not be shared.
    with create_sandbox(datasets, artifacts_dir=settings.artifacts_dir) as sb:
        ctx = ToolContext(tables=sb.tables, artifacts_dir=settings.artifacts_dir, sandbox=sb)
        llm = llm_factory(question, sb) if llm_factory else LLMClient(settings)
        # Nobody is at the keyboard during eval: with AGENT_APPROVAL_ENABLED on and no approver,
        # every gated step would be denied. Auto-approve by default (recorded as decided_by
        # "auto" in the trace); ``eval run --no-auto-approve`` measures the deny path instead.
        approver = AutoApprover() if auto_approve else None
        on_event = None
        if recorder is not None:
            llm = EventedLLM(llm, recorder.on_event, cost_fn=estimate_cost_usd)
            on_event = recorder.on_event
        loop = AgentLoop(
            llm, build_default_registry(), ctx, settings, approver=approver, on_event=on_event
        )
        return loop.run(question)


async def core_agent(
    inp: AgentInput, routing_enabled: bool | None = None, auto_approve: bool = True
) -> AgentOutput:
    """Run the real agent (``AgentLoop``, sync) in a worker thread.

    ``routing_enabled`` overrides ``AGENT_ROUTING_ENABLED`` for this run (eval A/B).
    ``auto_approve`` approves every human-in-the-loop gate (``AGENT_APPROVAL_ENABLED``).
    """
    recorder = make_recorder(inp)
    with log_context(task_id=inp.task_id):
        result = await asyncio.to_thread(
            _run_core,
            inp.question,
            list(inp.dataset_paths),
            None,
            routing_enabled,
            auto_approve,
            recorder,
        )
    return output_from_core(result, recorder)


def _dry_run_llm(question: str, sb: Any) -> Any:
    from analyst_agent.core.fake_llm import dry_run_llm

    return dry_run_llm(question, sb.tables[0], sb.run_sql)


async def dry_run_agent(
    inp: AgentInput, routing_enabled: bool | None = None, auto_approve: bool = True
) -> AgentOutput:
    """Run the real agent loop with the scripted offline LLM (no API calls)."""
    recorder = make_recorder(inp)
    with log_context(task_id=inp.task_id):
        result = await asyncio.to_thread(
            _run_core,
            inp.question,
            list(inp.dataset_paths),
            _dry_run_llm,
            routing_enabled,
            auto_approve,
            recorder,
        )
    return output_from_core(result, recorder)


@dataclass(frozen=True)
class CoreAgent:
    """``core`` / ``dry-run`` agent with per-run overrides of the AGENT_* feature flags.

    A module-level class (not a closure) so it pickles into ``isolation="process"`` children.
    ``None`` means "use the environment".
    """

    dry_run: bool = False
    routing_enabled: bool | None = None
    auto_approve: bool = True

    async def __call__(self, inp: AgentInput) -> AgentOutput:
        fn = dry_run_agent if self.dry_run else core_agent
        return await fn(inp, routing_enabled=self.routing_enabled, auto_approve=self.auto_approve)


def make_core_agent(
    routing_enabled: bool | None = None, dry_run: bool = False, auto_approve: bool = True
) -> AgentFn:
    """``core`` (or ``dry-run``) agent with model routing forced on/off (``None`` = env) and
    human-in-the-loop gates auto-approved (default) or left without an approver (denied)."""
    if routing_enabled is None and auto_approve:
        return dry_run_agent if dry_run else core_agent
    return CoreAgent(dry_run=dry_run, routing_enabled=routing_enabled, auto_approve=auto_approve)


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


def load_agent(
    spec: str,
    tasks: list[Task] | None = None,
    routing_enabled: bool | None = None,
    auto_approve: bool = True,
) -> AgentFn:
    """Resolve ``core``, ``oracle`` or ``package.module:attr`` into an agent callable.

    If ``attr`` is a class or zero-arg factory returning a callable, it is instantiated.
    ``routing_enabled`` and ``auto_approve`` only apply to ``core`` and ``dry-run``.
    """
    if spec == "core":
        return make_core_agent(routing_enabled, auto_approve=auto_approve)
    if spec == "dry-run":
        return make_core_agent(routing_enabled, dry_run=True, auto_approve=auto_approve)
    if spec == "oracle":
        return OracleAgent(tasks or [])
    if ":" not in spec:
        raise ValueError(f"unknown agent {spec!r}; use core, dry-run, oracle or module:callable")
    mod_name, attr = spec.split(":", 1)
    obj = getattr(importlib.import_module(mod_name), attr)
    if inspect.isclass(obj):
        obj = obj()
    return obj
