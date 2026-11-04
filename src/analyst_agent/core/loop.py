"""The agent loop: Plan -> Execute (tool calls) -> Verify (critic) -> Revise.

Guarantees:
* ``AgentLoop.run`` never raises. Every outcome is an :class:`AgentResult` with a
  :class:`RunStatus`; failures are reported in ``status`` / ``detail``.
* The run hard-stops when the cumulative token count reaches ``token_budget`` and returns
  whatever partial result exists (draft report or a summary of executed steps).
* At most ``max_turns`` agent LLM calls and ``max_critic_rounds`` critic reviews per run.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from analyst_agent.core.config import Settings, get_settings
from analyst_agent.core.critic import Critic, CriticVerdict
from analyst_agent.core.llm import LLM, CostTracker
from analyst_agent.core.prompts import (
    REPORT_WRITER_SYSTEM,
    planner_system,
    prompt_versions,
    render_metric_reference,
)
from analyst_agent.tools.base import ToolContext
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


@dataclass
class AgentState:
    question: str
    tables: list[str]
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
    status: RunStatus | None = None
    detail: str = ""
    # Semantic layer: whether it was on for this run and which metrics were injected.
    semantic_layer_enabled: bool = False
    retrieved_metrics: list[str] = field(default_factory=list)
    retrieved_scores: list[float] = field(default_factory=list)
    semantic_error: str | None = None

    @property
    def current_plan(self) -> str | None:
        return self.plan[-1] if self.plan else None


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
            "cost": s.cost.to_dict(),
            "prompts": prompt_versions(),
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
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.ctx = ctx
        self.settings = settings or get_settings()
        self.critic = critic or Critic(llm, model=self.settings.resolved_critic_model)
        self._on_event = on_event
        self._retriever = retriever

    # ------------------------------------------------------------------ public

    def run(self, question: str) -> AgentResult:
        state = AgentState(question=question, tables=list(self.ctx.tables))
        try:
            self._run(state)
        except Exception as exc:  # noqa: BLE001 - the loop must never raise
            log.exception("agent loop failed")
            state.status = RunStatus.ERROR
            state.detail = f"{type(exc).__name__}: {exc}"
        return self._result(state)

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

                retriever = default_retriever(s.semantic_metrics_dir)
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

    def _initial_message(self, question: str, reference: str = "") -> dict[str, Any]:
        tables = ", ".join(self.ctx.tables) or "(none)"
        ref = f"{reference}\n\n" if reference else ""
        return {
            "role": "user",
            "content": (
                f"Question: {question}\n\n"
                f"Loaded tables: {tables}\n\n"
                f"{ref}"
                "Start by writing your plan in <plan>...</plan>, then use the tools."
            ),
        }

    def _run(self, state: AgentState) -> None:
        s = self.settings
        reference = self._retrieve_metrics(state) if s.semantic_layer_enabled else ""
        state.messages.append(self._initial_message(state.question, reference))
        system = planner_system(s.semantic_layer_enabled)
        tools = self.registry.schemas()

        while True:
            if self._over_budget(state):
                return self._stop(
                    state,
                    RunStatus.BUDGET_EXCEEDED,
                    f"Token budget exhausted ({state.cost.total_tokens:,} / {s.token_budget:,}).",
                )
            if state.turns >= s.max_turns:
                return self._stop(state, RunStatus.MAX_TURNS, f"Reached max_turns={s.max_turns}.")

            state.turns += 1
            resp = self.llm.create(
                system=system,
                messages=state.messages,
                tools=tools,
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
            for tu in tool_uses:
                name, args = tu.get("name", ""), tu.get("input") or {}
                self._emit("tool_call", name=name, input=args, turn=state.turns)
                res = self.registry.execute(name, args, self.ctx)
                state.tool_results.append(
                    ToolCallRecord(
                        id=tu["id"],
                        name=name,
                        input=args if isinstance(args, dict) else {"raw": args},
                        output=res.content,
                        is_error=res.is_error,
                        turn=state.turns,
                        data=res.data,
                    )
                )
                self._emit(
                    "tool_result", name=name, is_error=res.is_error, summary=res.content[:300]
                )
                if res.data.get("path") and not res.is_error:
                    state.artifacts.append(res.data["path"])
                state.artifacts.extend(res.data.get("artifacts", []))

                if name == TERMINAL_TOOL and res.terminate and not res.is_error:
                    submitted = (tu, res.data)
                    continue  # its tool_result depends on the critic verdict
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

            if submitted is not None:
                tu, data = submitted
                state.draft_report = data["markdown"]
                state.key_numbers = dict(data["key_numbers"])
                done, feedback = self._verify(state)
                result_blocks.append(
                    {"type": "tool_result", "tool_use_id": tu["id"], "content": feedback}
                )
                state.messages.append({"role": "user", "content": result_blocks})
                if done:
                    return
                continue  # Revise: the agent re-plans with the critic's feedback

            state.messages.append({"role": "user", "content": result_blocks})

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
        try:
            verdict = self.critic.review(
                state.question,
                state.draft_report or "",
                state.key_numbers,
                [asdict(r) for r in state.tool_results],
                cost=state.cost,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("critic failed")
            verdict = CriticVerdict(verdict="pass", parse_error=f"critic call failed: {exc}")
            state.critic_reviews.append(verdict.to_dict())
            self._stop(state, RunStatus.COMPLETED_WITH_ISSUES, "Critic unavailable; unverified.")
            return True, "Report accepted (review unavailable)."

        state.critic_reviews.append(verdict.to_dict())
        self._emit("critic", round=state.critic_rounds, **verdict.to_dict())

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

        if state.critic_rounds >= s.max_critic_rounds:
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
