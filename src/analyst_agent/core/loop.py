"""The agent loop: Plan -> Execute (tool calls) -> Verify (critic) -> Revise.

Guarantees:
* ``AgentLoop.run`` never raises. Every outcome is an :class:`AgentResult` with a
  :class:`RunStatus`; failures are reported in ``status`` / ``detail``.
* The run hard-stops when the cumulative token count reaches ``token_budget`` and returns
  whatever partial result exists (draft report or a summary of executed steps).
* At most ``max_turns`` agent LLM calls and ``max_critic_rounds`` critic reviews per run,
  plus one extra of each per escalation when routing is enabled.

Routing (``AGENT_ROUTING_ENABLED``, see :mod:`analyst_agent.core.router`): a cheap router call
picks the tier and working model before the first turn. On a second critic "revise" or a
repeated tool error, the loop escalates to the next tier and retries the step once with the
stronger model. With routing off, every agent call uses ``AGENT_MODEL`` exactly as before.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from analyst_agent.core.approval import (
    ApprovalDecision,
    ApprovalPolicy,
    ApprovalRecord,
    Approver,
    denial_message,
)
from analyst_agent.core.config import Settings, get_settings
from analyst_agent.core.critic import Critic, CriticVerdict
from analyst_agent.core.llm import LLM, CostTracker
from analyst_agent.core.logs import log_context
from analyst_agent.core.memory import InsightStore, dataset_fingerprint, insight_from_result
from analyst_agent.core.prompts import (
    REPORT_WRITER_SYSTEM,
    planner_system,
    prompt_versions,
    render_metric_reference,
    render_prior_insights,
)
from analyst_agent.core.report import (
    StructuredReport,
    check_consistency,
    missing_chart_paths,
    structured_final_report_schema,
)
from analyst_agent.core.router import (
    Escalation,
    EscalationTrigger,
    RouteDecision,
    Router,
    Tier,
    next_tier,
    schema_summary,
)
from analyst_agent.core.tracing import new_run_id
from analyst_agent.tools.base import ToolContext, ToolResult
from analyst_agent.tools.registry import TERMINAL_TOOL, ToolRegistry, wrap_untrusted

if TYPE_CHECKING:
    from analyst_agent.semantic import MetricRetriever

log = logging.getLogger(__name__)

_PLAN_RE = re.compile(r"<plan>(.*?)</plan>", re.DOTALL | re.IGNORECASE)

NUDGE = (
    "Continue the analysis using the tools. When the answer is verified, call final_report "
    "with the Markdown report and key_numbers."
)


class RunStatus(StrEnum):
    COMPLETED = "completed"  # critic passed
    COMPLETED_WITH_ISSUES = "completed_with_issues"  # critic rounds exhausted / critic failed
    BUDGET_EXCEEDED = "budget_exceeded"  # token budget hit; partial result
    MAX_TURNS = "max_turns"  # turn limit hit; partial result
    REFUSED = "refused"  # model declined
    ERROR = "error"  # unexpected failure (e.g. API unreachable after retries)


@dataclass
class ToolCallRecord:
    id: str
    name: str
    input: dict[str, Any]
    output: str
    is_error: bool
    turn: int
    data: dict[str, Any] = field(default_factory=dict)
    model: str | None = None  # agent model that issued the call (None = AGENT_MODEL default)


@dataclass
class AgentState:
    question: str
    tables: list[str]
    run_id: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[ToolCallRecord] = field(default_factory=list)
    plan: list[str] = field(default_factory=list)  # every <plan> the agent wrote, in order
    cost: CostTracker = field(default_factory=CostTracker)
    turns: int = 0
    critic_rounds: int = 0
    critic_reviews: list[dict[str, Any]] = field(default_factory=list)
    draft_report: str | None = None
    key_numbers: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    approvals: list[ApprovalRecord] = field(default_factory=list)
    budget_approved: bool = False  # the budget-fraction gate was approved once this run
    status: RunStatus | None = None
    detail: str = ""
    # Semantic layer: whether it was on for this run and which metrics were injected.
    semantic_layer_enabled: bool = False
    retrieved_metrics: list[str] = field(default_factory=list)
    retrieved_scores: list[float] = field(default_factory=list)
    semantic_error: str | None = None
    # Structured report (AGENT_STRUCTURED_REPORT_ENABLED): the parsed fields of the latest
    # submission and one consistency check per submission.
    structured_report: dict[str, Any] | None = None
    report_checks: list[dict[str, Any]] = field(default_factory=list)
    # Insight memory (AGENT_MEMORY_ENABLED)
    memory_enabled: bool = False
    dataset_fingerprint: str | None = None
    prior_insights: list[dict[str, Any]] = field(default_factory=list)
    memory_saved: bool = False
    memory_error: str | None = None
    # Routing (all empty / None when routing is disabled)
    route: RouteDecision | None = None
    tier: Tier | None = None
    model: str | None = None  # current working model; None = the configured default
    escalations: list[Escalation] = field(default_factory=list)
    turn_models: list[str | None] = field(default_factory=list)  # model used for each agent turn
    extra_turns: int = 0  # granted by escalations ("retry the step once")
    extra_critic_rounds: int = 0
    critic_revisions: int = 0
    last_tool_error: dict[str, bool] = field(default_factory=dict)  # tool name -> last errored

    @property
    def current_plan(self) -> str | None:
        return self.plan[-1] if self.plan else None

    def routing_summary(self) -> dict[str, Any] | None:
        if self.route is None:
            return None
        return {
            "enabled": True,
            "routed_tier": self.route.tier.value,
            "routed_model": self.route.model,
            "final_tier": self.tier.value if self.tier else None,
            "final_model": self.model,
            "route": self.route.to_dict(),
            "escalations": [e.to_dict() for e in self.escalations],
            "turn_models": list(self.turn_models),
        }


@dataclass
class AgentResult:
    status: RunStatus
    report: str
    key_numbers: dict[str, Any]
    state: AgentState

    @property
    def ok(self) -> bool:
        return self.status in (RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_ISSUES)

    @property
    def cost(self) -> CostTracker:
        return self.state.cost

    def to_dict(self) -> dict[str, Any]:
        s = self.state
        return {
            "run_id": s.run_id,
            "status": self.status.value,
            "detail": s.detail,
            "report": self.report,
            "key_numbers": self.key_numbers,
            "plan": s.plan,
            "turns": s.turns,
            "critic_rounds": s.critic_rounds,
            "critic_reviews": s.critic_reviews,
            "tool_calls": [asdict(r) for r in s.tool_results],
            "artifacts": s.artifacts,
            "semantic_layer": {
                "enabled": s.semantic_layer_enabled,
                "retrieved_metrics": s.retrieved_metrics,
                "scores": s.retrieved_scores,
                "error": s.semantic_error,
            },
            "approvals": [asdict(a) for a in s.approvals],
            "structured_report": s.structured_report,
            "report_checks": s.report_checks,
            "memory": {
                "enabled": s.memory_enabled,
                "fingerprint": s.dataset_fingerprint,
                "prior_insights": s.prior_insights,
                "saved": s.memory_saved,
                "error": s.memory_error,
            },
            "cost": s.cost.to_dict(),
            "prompts": prompt_versions(),
            "routing": s.routing_summary(),
        }


EventHook = Callable[[str, dict[str, Any]], None]


class AgentLoop:
    def __init__(
        self,
        llm: LLM,
        registry: ToolRegistry,
        ctx: ToolContext,
        settings: Settings | None = None,
        critic: Critic | None = None,
        on_event: EventHook | None = None,
        retriever: MetricRetriever | None = None,
        router: Router | None = None,
        approver: Approver | None = None,
        policy: ApprovalPolicy | None = None,
        memory: InsightStore | None = None,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.ctx = ctx
        self.settings = settings or get_settings()
        self.critic = critic or Critic(llm, model=self.settings.resolved_critic_model)
        self._on_event = on_event
        self._retriever = retriever
        if router is None and self.settings.routing_enabled:
            router = Router(llm, self.settings)
        self.router = router
        self.approver = approver
        self.policy = policy or ApprovalPolicy.from_settings(self.settings)
        self._memory = memory

    # ------------------------------------------------------------------ public

    def run(self, question: str, run_id: str | None = None) -> AgentResult:
        """Answer ``question``. ``run_id`` correlates logs and traces; generated when omitted."""
        state = AgentState(
            question=question, tables=list(self.ctx.tables), run_id=run_id or new_run_id()
        )
        with log_context(run_id=state.run_id):
            s = self.settings
            log.info("run started", extra={"tables": state.tables, "model": s.model})
            self._emit(
                "run_start",
                run_id=state.run_id,
                question=question,
                tables=state.tables,
                model=s.model,
                max_turns=s.max_turns,
                token_budget=s.token_budget,
                flags={
                    "routing": s.routing_enabled,
                    "semantic_layer": s.semantic_layer_enabled,
                    "approval": s.approval_enabled,
                    "structured_report": s.structured_report_enabled,
                    "memory": s.memory_enabled,
                },
                prompts=prompt_versions(),
            )
            try:
                self._run(state)
            except Exception as exc:  # noqa: BLE001 - the loop must never raise
                log.exception("agent loop failed")
                state.status = RunStatus.ERROR
                state.detail = f"{type(exc).__name__}: {exc}"
            if state.memory_enabled and state.status is RunStatus.COMPLETED:
                self._remember(state)
            result = self._result(state)
            log.info(
                "run finished",
                extra={
                    "status": result.status.value,
                    "turns": state.turns,
                    "cost_usd": round(state.cost.usd, 6),
                    "total_tokens": state.cost.total_tokens,
                },
            )
            return result

    # ------------------------------------------------------------------ internals

    def _emit(self, kind: str, **payload: Any) -> None:
        if self._on_event is not None:
            try:
                self._on_event(kind, payload)
            except Exception:  # noqa: BLE001 - observers must not break the run
                log.exception("event hook failed")

    def _over_budget(self, state: AgentState) -> bool:
        return state.cost.total_tokens >= self.settings.token_budget

    def _stop(self, state: AgentState, status: RunStatus, detail: str) -> None:
        if status not in (RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_ISSUES):
            log.warning("run stopping early", extra={"status": status.value, "detail": detail})
        state.status = status
        state.detail = detail
        self._emit("stop", status=status.value, detail=detail)

    def _retrieve_metrics(self, state: AgentState) -> str:
        """Semantic layer: retrieve relevant metric definitions; return the reference block.

        Failures (missing / invalid metrics files) never fail the run: the error is recorded on
        the state and the agent proceeds without reference material.
        """
        s = self.settings
        state.semantic_layer_enabled = True
        try:
            retriever = self._retriever
            if retriever is None:
                from analyst_agent.semantic import default_retriever

                retriever = default_retriever(
                    s.resolved_semantic_metrics_dir,
                    backend=s.semantic_backend,
                    embedding_model=s.semantic_embedding_model,
                    cache_dir=s.semantic_embedding_cache_dir,
                    hybrid_weight=s.semantic_hybrid_weight,
                )
            hits = retriever.retrieve(state.question, k=s.semantic_top_k, tables=self.ctx.tables)
        except Exception as exc:  # noqa: BLE001 - optional context must not break the run
            log.exception("semantic layer retrieval failed")
            state.semantic_error = f"{type(exc).__name__}: {exc}"
            hits = []
        state.retrieved_metrics = [h.metric.name for h in hits]
        state.retrieved_scores = [round(h.score, 4) for h in hits]
        self._emit(
            "semantic_retrieval",
            metrics=state.retrieved_metrics,
            scores=state.retrieved_scores,
            error=state.semantic_error,
        )
        return render_metric_reference([h.metric for h in hits])

    # ------------------------------------------------------------------ insight memory

    def _memory_store(self) -> InsightStore:
        if self._memory is None:
            self._memory = InsightStore(self.settings.memory_db_path)
        return self._memory

    def _recall(self, state: AgentState) -> str:
        """Retrieve prior insights for this dataset; return the labelled context block.

        Failures never fail the run: the error is recorded and the agent proceeds without memory.
        """
        state.memory_enabled = True
        try:
            state.dataset_fingerprint = dataset_fingerprint(self.ctx)
            hits = self._memory_store().search(
                state.dataset_fingerprint, state.question, k=self.settings.memory_top_k
            )
        except Exception as exc:  # noqa: BLE001 - optional context must not break the run
            log.exception("insight memory retrieval failed")
            state.memory_error = f"{type(exc).__name__}: {exc}"
            hits = []
        state.prior_insights = [h.to_dict() for h in hits]
        self._emit(
            "memory_retrieval",
            fingerprint=state.dataset_fingerprint,
            questions=[h.question for h in hits],
            scores=[h.score for h in hits],
            error=state.memory_error,
        )
        return render_prior_insights(hits)

    def _remember(self, state: AgentState) -> None:
        """Save a critic-approved run's findings. Never raises."""
        if state.dataset_fingerprint is None:
            return
        try:
            insight = insight_from_result(
                state.dataset_fingerprint,
                state.question,
                state.draft_report or "",
                state.key_numbers,
                state.structured_report,
            )
            if insight is not None:
                self._memory_store().add(insight)
                state.memory_saved = True
        except Exception as exc:  # noqa: BLE001
            log.exception("insight memory save failed")
            state.memory_error = f"{type(exc).__name__}: {exc}"
        self._emit("memory_saved", saved=state.memory_saved, error=state.memory_error)

    # ------------------------------------------------------------------ structured report

    def _enforce_structured_report(self, state: AgentState, res: ToolResult) -> ToolResult:
        """With structured reports on, reject free-Markdown submissions and unknown charts."""
        structured = res.data.get("structured")
        if structured is None:
            return ToolResult(
                "final_report must use the structured fields: headline, key_numbers, method, "
                "caveats and charts (see the tool schema). Submit again in that form.",
                is_error=True,
            )
        missing = missing_chart_paths(StructuredReport.from_dict(structured), state.artifacts)
        if missing:
            return ToolResult(
                "These chart paths were not produced by a tool in this run: "
                + ", ".join(missing)
                + ". Create them with make_chart and use the returned path, or drop them.",
                is_error=True,
            )
        return res

    def _consistency_notes(self, state: AgentState) -> str:
        """Run the headline / key-number consistency check on the current structured draft."""
        if state.structured_report is None:
            return ""
        check = check_consistency(
            StructuredReport.from_dict(state.structured_report),
            [asdict(r) for r in state.tool_results],
            question=state.question,
        )
        state.report_checks.append(check.to_dict())
        self._emit("report_check", **check.to_dict())
        return check.as_critic_notes()

    def _initial_message(
        self, question: str, reference: str = "", prior: str = ""
    ) -> dict[str, Any]:
        tables = ", ".join(self.ctx.tables) or "(none)"
        ref = "".join(f"{block}\n\n" for block in (reference, prior) if block)
        return {
            "role": "user",
            "content": (
                f"Question: {question}\n\n"
                f"Loaded tables: {tables}\n\n"
                f"{ref}"
                "Start by writing your plan in <plan>...</plan>, then use the tools."
            ),
        }

    # ------------------------------------------------------------------ routing

    def _route(self, state: AgentState) -> None:
        assert self.router is not None
        decision = self.router.classify(state.question, schema_summary(self.ctx), cost=state.cost)
        state.route, state.tier, state.model = decision, decision.tier, decision.model
        self._emit("route", **decision.to_dict())

    def _escalate(self, state: AgentState, trigger: EscalationTrigger, detail: str) -> bool:
        """Move to the next tier and grant one retry of the current step.

        Returns False (and changes nothing) when routing is off or the run is already at L3.
        """
        if self.router is None or state.tier is None:
            return False
        to = next_tier(state.tier)
        if to is None:
            return False
        esc = Escalation(
            from_tier=state.tier,
            to_tier=to,
            from_model=state.model or self.settings.model,
            to_model=self.router.model_for(to),
            trigger=trigger,
            turn=state.turns,
            detail=detail,
        )
        state.escalations.append(esc)
        state.tier, state.model = to, esc.to_model
        state.extra_turns += 1  # the retry must not be starved by max_turns
        self._emit("escalation", **esc.to_dict())
        return True

    def _check_repeated_tool_errors(self, state: AgentState, records: list[ToolCallRecord]) -> None:
        """Escalate when a tool errors on two consecutive calls of that tool."""
        repeated: list[str] = []
        for r in records:
            if "approval" in r.data:
                continue  # a human denial is not a model failure; never escalate on it
            if r.is_error and state.last_tool_error.get(r.name):
                repeated.append(r.name)
            state.last_tool_error[r.name] = r.is_error
        if repeated and self._escalate(
            state, "tool_error", f"repeated error from {', '.join(sorted(set(repeated)))}"
        ):
            state.last_tool_error.clear()  # the escalated model starts with a clean slate

    # ------------------------------------------------------------------ main loop

    def _run(self, state: AgentState) -> None:
        s = self.settings
        if self.router is not None:
            self._route(state)
        reference = self._retrieve_metrics(state) if s.semantic_layer_enabled else ""
        prior = self._recall(state) if s.memory_enabled else ""
        state.messages.append(self._initial_message(state.question, reference, prior))
        system = planner_system(
            s.semantic_layer_enabled, s.structured_report_enabled, s.memory_enabled
        )
        tools = self.registry.schemas()
        if s.structured_report_enabled:
            tools = [
                structured_final_report_schema() if t["name"] == TERMINAL_TOOL else t for t in tools
            ]

        while True:
            if self._over_budget(state):
                return self._stop(
                    state,
                    RunStatus.BUDGET_EXCEEDED,
                    f"Token budget exhausted ({state.cost.total_tokens:,} / {s.token_budget:,}).",
                )
            if state.turns >= s.max_turns + state.extra_turns:
                return self._stop(
                    state,
                    RunStatus.MAX_TURNS,
                    f"Reached max_turns={s.max_turns}"
                    + (f" (+{state.extra_turns} for escalation)" if state.extra_turns else "")
                    + ".",
                )

            state.turns += 1
            state.turn_models.append(state.model)
            self._emit(
                "turn",
                turn=state.turns,
                model=state.model or s.model,
                tier=state.tier.value if state.tier else None,
            )
            resp = self.llm.create(
                system=system,
                messages=state.messages,
                tools=tools,
                model=state.model,
                role="agent",
                cost=state.cost,
            )
            # Append the full content (incl. thinking blocks) unchanged.
            state.messages.append({"role": "assistant", "content": resp.content})

            for m in _PLAN_RE.finditer(resp.text):
                state.plan.append(m.group(1).strip())
                self._emit("plan", plan=state.plan[-1], turn=state.turns)

            if resp.stop_reason == "refusal":
                return self._stop(state, RunStatus.REFUSED, "The model declined this request.")

            tool_uses = resp.tool_uses
            if not tool_uses:
                state.messages.append({"role": "user", "content": NUDGE})
                continue

            result_blocks: list[dict[str, Any]] = []
            submitted: tuple[dict[str, Any], dict[str, Any]] | None = None  # (tool_use, data)
            turn_records: list[ToolCallRecord] = []
            for tu in tool_uses:
                name, args = tu.get("name", ""), tu.get("input") or {}
                log.info("tool call", extra={"tool": name, "turn": state.turns})
                self._emit("tool_call", name=name, input=args, turn=state.turns)
                denied = self._gate(state, name, args)
                res = denied or self.registry.execute(name, args, self.ctx)
                if name == TERMINAL_TOOL and s.structured_report_enabled and not res.is_error:
                    res = self._enforce_structured_report(state, res)
                record = ToolCallRecord(
                    id=tu["id"],
                    name=name,
                    input=args if isinstance(args, dict) else {"raw": args},
                    output=res.content,
                    is_error=res.is_error,
                    turn=state.turns,
                    data=res.data,
                    model=state.model,
                )
                state.tool_results.append(record)
                turn_records.append(record)
                self._emit(
                    "tool_result", name=name, is_error=res.is_error, summary=res.content[:300]
                )
                if res.data.get("path") and not res.is_error:
                    state.artifacts.append(res.data["path"])
                state.artifacts.extend(res.data.get("artifacts", []))

                if name == TERMINAL_TOOL and res.terminate and not res.is_error:
                    if submitted is None:
                        submitted = (tu, res.data)
                        continue  # its tool_result depends on the critic verdict
                    # A second submission in the same response still needs a tool_result,
                    # or the next request is rejected for an unanswered tool_use id.
                    res = ToolResult(
                        "final_report was already called in this response; only the first "
                        "submission is reviewed.",
                        is_error=True,
                    )
                    record.output, record.is_error = res.content, True
                result_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu["id"],
                        "content": wrap_untrusted(
                            name,
                            res.content,
                            is_error=res.is_error,
                            max_chars=s.max_tool_output_chars,
                        ),
                        "is_error": res.is_error,
                    }
                )

            # Escalate before the next turn so the stronger model sees the errors and retries.
            self._check_repeated_tool_errors(state, turn_records)

            if submitted is not None:
                tu, data = submitted
                state.draft_report = data["markdown"]
                state.key_numbers = dict(data["key_numbers"])
                state.structured_report = data.get("structured")
                done, feedback = self._verify(state)
                result_blocks.append(
                    {"type": "tool_result", "tool_use_id": tu["id"], "content": feedback}
                )
                state.messages.append({"role": "user", "content": result_blocks})
                if done:
                    return
                continue  # Revise: the agent re-plans with the critic's feedback

            state.messages.append({"role": "user", "content": result_blocks})

    def _gate(self, state: AgentState, name: str, args: Any) -> ToolResult | None:
        """Human-in-the-loop check. Returns an error result if the step was denied."""
        action = self.policy.evaluate(
            name,
            args,
            self.ctx,
            tokens_used=state.cost.total_tokens,
            token_budget=self.settings.token_budget,
            budget_approved=state.budget_approved,
            turn=state.turns,
        )
        if action is None:
            return None
        self._emit("approval_required", **action.to_dict())
        if self.approver is None:
            decision = ApprovalDecision(approved=False, decided_by="unavailable")
        else:
            try:
                decision = self.approver.request(action)
            except Exception as exc:  # noqa: BLE001 - a broken approver denies, never crashes
                log.exception("approver failed")
                decision = ApprovalDecision(
                    approved=False, decided_by="unavailable", note=f"approver error: {exc}"
                )
        state.approvals.append(ApprovalRecord(action=action.to_dict(), decision=decision.to_dict()))
        self._emit(
            "approval_decision",
            action_id=action.id,
            tool=name,
            turn=state.turns,
            **decision.to_dict(),
        )
        if decision.approved:
            if "budget" in action.triggers:
                state.budget_approved = True
            return None
        return ToolResult(
            denial_message(action, decision),
            is_error=True,
            data={"approval": {"action_id": action.id, **decision.to_dict()}},
        )

    def _verify(self, state: AgentState) -> tuple[bool, str]:
        """Run the critic on the current draft. Returns (done, tool_result text for the agent)."""
        s = self.settings
        if self._over_budget(state):
            self._stop(
                state,
                RunStatus.BUDGET_EXCEEDED,
                "Token budget exhausted before the critic could review the draft (unverified).",
            )
            return True, "Report accepted without review (budget exhausted)."

        state.critic_rounds += 1
        notes = self._consistency_notes(state)
        try:
            verdict = self.critic.review(
                state.question,
                state.draft_report or "",
                state.key_numbers,
                [asdict(r) for r in state.tool_results],
                cost=state.cost,
                **({"consistency_notes": notes} if notes else {}),
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("critic failed")
            verdict = CriticVerdict(verdict="pass", parse_error=f"critic call failed: {exc}")
            state.critic_reviews.append(verdict.to_dict())
            self._stop(state, RunStatus.COMPLETED_WITH_ISSUES, "Critic unavailable; unverified.")
            return True, "Report accepted (review unavailable)."

        state.critic_reviews.append(verdict.to_dict())
        self._emit("critic", round=state.critic_rounds, **verdict.to_dict())
        if verdict.verdict == "revise":
            state.critic_revisions += 1
            # Second revise: hand the revision to a stronger model, with one extra review.
            if state.critic_revisions == 2 and self._escalate(
                state, "critic_revise", "; ".join(verdict.issues)[:500]
            ):
                state.extra_critic_rounds += 1

        if verdict.verdict == "pass":
            if verdict.parse_error:
                self._stop(
                    state,
                    RunStatus.COMPLETED_WITH_ISSUES,
                    f"Critic output could not be parsed ({verdict.parse_error}); unverified.",
                )
            else:
                self._stop(state, RunStatus.COMPLETED, "Critic verdict: pass.")
            return True, "Report accepted."

        if state.critic_rounds >= s.max_critic_rounds + state.extra_critic_rounds:
            self._stop(
                state,
                RunStatus.COMPLETED_WITH_ISSUES,
                f"Critic still requested revision after {state.critic_rounds} round(s): "
                + "; ".join(verdict.issues),
            )
            return True, "Report accepted with unresolved reviewer issues."

        return False, verdict.as_feedback()

    # ------------------------------------------------------------------ results

    def _result(self, state: AgentState) -> AgentResult:
        status = state.status or RunStatus.ERROR
        if status in (RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_ISSUES):
            report = state.draft_report or ""
            if status is RunStatus.COMPLETED_WITH_ISSUES:
                report = _banner(status, state.detail) + report
        else:
            report = self._partial_report(state, status)
        return AgentResult(status=status, report=report, key_numbers=state.key_numbers, state=state)

    def _partial_report(self, state: AgentState, status: RunStatus) -> str:
        banner = _banner(status, state.detail)
        if state.draft_report:
            return banner + state.draft_report
        if status is RunStatus.MAX_TURNS and state.tool_results and not self._over_budget(state):
            try:
                return banner + self._synthesize(state)
            except Exception:  # noqa: BLE001
                log.exception("report writer failed; falling back to step summary")
        return banner + _steps_summary(state)

    def _synthesize(self, state: AgentState) -> str:
        """One tool-free call with the report-writer prompt, over the evidence gathered so far."""
        parts = [f"## Question\n{state.question}", f"## Plan\n{state.current_plan or '(none)'}"]
        for r in state.tool_results:
            if r.name == TERMINAL_TOOL:
                continue
            src = r.input.get("query") or r.input.get("code") or r.input
            parts.append(
                f"### {r.name}\n```\n{src}\n```\n"
                + wrap_untrusted(r.name, r.output, is_error=r.is_error, max_chars=4000)
            )
        resp = self.llm.create(
            system=REPORT_WRITER_SYSTEM,
            messages=[{"role": "user", "content": "\n\n".join(parts)}],
            model=state.model,
            role="report_writer",
            cost=state.cost,
        )
        return resp.text


def _banner(status: RunStatus, detail: str) -> str:
    return f"> **Status: {status.value}** — {detail}\n\n"


def _steps_summary(state: AgentState) -> str:
    lines = ["## Partial results", ""]
    if state.current_plan:
        lines += ["### Plan", state.current_plan, ""]
    ok = [r for r in state.tool_results if not r.is_error and r.name != TERMINAL_TOOL]
    if not ok:
        lines.append("No analysis steps completed successfully before the run stopped.")
        return "\n".join(lines)
    lines.append("### Executed steps (most recent last)")
    for r in ok[-5:]:
        src = r.input.get("query") or r.input.get("code")
        lines.append(f"\n**{r.name}**")
        if src:
            lines.append(f"```\n{src}\n```")
        out = r.output if len(r.output) <= 2000 else r.output[:2000] + "\n... [truncated]"
        lines.append(out)
    return "\n".join(lines)
