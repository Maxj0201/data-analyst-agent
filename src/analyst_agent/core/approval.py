"""Human-in-the-loop approval for costly or risky agent steps.

Policy (all off unless ``AGENT_APPROVAL_ENABLED=true``):

(a) ``run_sql`` whose DuckDB ``EXPLAIN`` row estimate exceeds ``AGENT_APPROVAL_ROW_THRESHOLD``;
(b) every ``run_python`` call (``AGENT_APPROVAL_PYTHON``, on by default once enabled);
(c) any tool call once the run's token spend exceeds ``AGENT_APPROVAL_BUDGET_FRACTION`` of
    ``AGENT_TOKEN_BUDGET`` (asked once: after an approval it is not asked again in that run).

``final_report`` is never gated: it carries no data access, and blocking it would only make the
model resubmit.

When a step needs approval the loop emits ``approval_required`` with a :class:`PendingAction`,
asks its :class:`Approver`, and emits ``approval_decision``. A denial (including a timeout) is
returned to the model as a tool error so it can re-plan. Approvers:

* :class:`AutoApprover` - approves everything (``analyst-agent ask --auto-approve``).
* :class:`CallbackApprover` - wraps a blocking ``action -> bool | (bool, note)`` function
  (the CLI's interactive y/n prompt).
* :class:`ApprovalBroker` - for the API: each job waits on an ``asyncio.Event`` with timeout
  ``AGENT_APPROVAL_TIMEOUT_S`` until ``POST /jobs/{id}/approve`` resolves it.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import itertools
import re
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol

from analyst_agent.sandbox.duckdb_runner import _strip_leading_noise
from analyst_agent.tools.base import ToolContext

DecidedBy = Literal["human", "auto", "timeout", "unavailable"]
Trigger = Literal["sql_rows", "python", "budget"]

GATED_SQL_TOOL = "run_sql"
GATED_PYTHON_TOOL = "run_python"
UNGATED_TOOLS = frozenset({"final_report"})

_ids = itertools.count(1)

# Leading ``EXPLAIN`` / ``EXPLAIN ANALYZE`` (after comments and whitespace are stripped).
_EXPLAIN_RE = re.compile(r"EXPLAIN\b\s*(ANALY[SZ]E\b)?\s*", re.IGNORECASE)

# DuckDB >= 1.1 prints "~1,234 rows" per operator; older versions print "EC: 1234".
_EST_RE = re.compile(r"~\s*([\d,]+)\s+rows?\b|\bEC:\s*([\d,]+)", re.IGNORECASE)


# --------------------------------------------------------------------------- data types


@dataclass
class PendingAction:
    """A tool call waiting for a human decision."""

    id: str
    tool: str
    input: dict[str, Any]
    reasons: list[str]
    summary: str
    triggers: list[Trigger] = field(default_factory=list)
    turn: int = 0
    estimated_rows: int | None = None
    tokens_used: int | None = None
    token_budget: int | None = None
    timeout_s: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ApprovalDecision:
    approved: bool
    decided_by: DecidedBy = "human"
    note: str | None = None
    waited_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ApprovalRecord:
    """One entry of the run trace (``AgentResult.to_dict()["approvals"]``)."""

    action: dict[str, Any]
    decision: dict[str, Any]
    ts: float = field(default_factory=time.time)


class Approver(Protocol):
    def request(self, action: PendingAction) -> ApprovalDecision: ...


# --------------------------------------------------------------------------- policy


def estimate_rows(plan_text: str) -> int | None:
    """Largest per-operator cardinality estimate in a DuckDB EXPLAIN plan (``None`` if absent)."""
    values = [int((a or b).replace(",", "")) for a, b in _EST_RE.findall(plan_text)]
    return max(values) if values else None


def _explain_text(rows: list[dict[str, Any]]) -> str:
    return "\n".join(str(v) for r in rows for v in r.values())


def _clip(text: str, n: int = 400) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


@dataclass
class ApprovalPolicy:
    """Decides whether a tool call needs approval. Disabled policies never ask."""

    enabled: bool = False
    row_threshold: int | None = None
    python: bool = True
    budget_fraction: float | None = None
    timeout_s: float = 300.0

    @classmethod
    def from_settings(cls, settings: Any) -> ApprovalPolicy:
        return cls(
            enabled=bool(getattr(settings, "approval_enabled", False)),
            row_threshold=getattr(settings, "approval_row_threshold", None),
            python=bool(getattr(settings, "approval_python", True)),
            budget_fraction=getattr(settings, "approval_budget_fraction", None),
            timeout_s=float(getattr(settings, "approval_timeout_s", 300.0)),
        )

    def evaluate(
        self,
        tool: str,
        args: dict[str, Any],
        ctx: ToolContext,
        *,
        tokens_used: int,
        token_budget: int,
        budget_approved: bool = False,
        turn: int = 0,
    ) -> PendingAction | None:
        """Return a :class:`PendingAction` if this call needs approval, else ``None``."""
        if not self.enabled or tool in UNGATED_TOOLS or not isinstance(args, dict):
            return None
        reasons: list[str] = []
        triggers: list[Trigger] = []
        estimated: int | None = None

        if tool == GATED_SQL_TOOL and self.row_threshold is not None:
            estimated = self.sql_estimate(str(args.get("query", "")), ctx)
            if estimated is not None and estimated > self.row_threshold:
                triggers.append("sql_rows")
                reasons.append(
                    f"EXPLAIN estimates {estimated:,} rows (threshold {self.row_threshold:,})"
                )

        if tool == GATED_PYTHON_TOOL and self.python:
            triggers.append("python")
            reasons.append("run_python executes model-written code")

        if (
            self.budget_fraction is not None
            and not budget_approved
            and token_budget > 0
            and tokens_used > self.budget_fraction * token_budget
        ):
            triggers.append("budget")
            reasons.append(
                f"token spend {tokens_used:,} exceeds {self.budget_fraction:.0%} of the "
                f"{token_budget:,} budget"
            )

        if not reasons:
            return None
        return PendingAction(
            id=f"appr_{next(_ids)}",
            tool=tool,
            input=args,
            reasons=reasons,
            summary=_summarise(tool, args, reasons),
            triggers=triggers,
            turn=turn,
            estimated_rows=estimated,
            tokens_used=tokens_used,
            token_budget=token_budget,
            timeout_s=self.timeout_s,
        )

    @staticmethod
    def sql_estimate(query: str, ctx: ToolContext) -> int | None:
        """Row estimate from ``EXPLAIN <query>``; ``None`` if it cannot be computed.

        A query DuckDB cannot plan will fail identically when executed, so it is not gated.
        """
        q = _strip_leading_noise(query).strip().rstrip(";").strip()
        m = _EXPLAIN_RE.match(q)
        if m:
            if not m.group(1):
                return None  # plain EXPLAIN only plans; nothing is executed
            q = q[m.end() :]  # EXPLAIN ANALYZE runs the query: gate on the inner query
        if not q:
            return None
        res = ctx.run_sql(f"EXPLAIN {q}")
        if not res.ok:
            return None
        return estimate_rows(_explain_text(res.rows))


def _summarise(tool: str, args: dict[str, Any], reasons: list[str]) -> str:
    src = args.get("query") or args.get("code") or args
    return f"{tool}: {_clip(str(src))} — needs approval because " + "; ".join(reasons) + "."


def denial_message(action: PendingAction, decision: ApprovalDecision) -> str:
    """Tool-error text the model sees for a denied step."""
    why = {
        "timeout": f"no decision within {action.timeout_s:g}s (treated as denied)",
        "unavailable": "no approver is configured",
    }.get(decision.decided_by, "denied by the human reviewer")
    msg = f"Step not executed: {why}. It was gated because " + "; ".join(action.reasons) + "."
    if decision.note:
        msg += f" Reviewer note: {decision.note}"
    return msg + " Re-plan: use a cheaper or narrower approach, or finish with what you have."


# --------------------------------------------------------------------------- approvers


class AutoApprover:
    def request(self, action: PendingAction) -> ApprovalDecision:
        return ApprovalDecision(approved=True, decided_by="auto")


class CallbackApprover:
    """Blocking approver around ``fn(action) -> bool | (bool, note | None)``."""

    def __init__(self, fn: Callable[[PendingAction], bool | tuple[bool, str | None]]) -> None:
        self._fn = fn

    def request(self, action: PendingAction) -> ApprovalDecision:
        started = time.monotonic()
        out = self._fn(action)
        approved, note = out if isinstance(out, tuple) else (out, None)
        return ApprovalDecision(
            approved=bool(approved),
            note=note,
            waited_s=round(time.monotonic() - started, 3),
        )


@dataclass
class _Waiter:
    action: PendingAction
    event: asyncio.Event
    decision: ApprovalDecision | None = None


class ApprovalBroker:
    """Routes approval requests from agent threads to the API, keyed by job id.

    Waits happen on ``asyncio.Event`` objects owned by a private event loop on a daemon thread,
    so agent worker threads, the FastAPI event loop and tests can all call in safely:
    :meth:`request` blocks the calling (worker) thread; :meth:`resolve` may be called from any
    thread, including from inside another running event loop.
    """

    def __init__(self, timeout_s: float = 300.0) -> None:
        self.timeout_s = timeout_s
        self._lock = threading.Lock()
        self._waiters: dict[str, _Waiter] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._closed = False

    # ---- loop management

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None and not self._closed:
                loop = asyncio.new_event_loop()
                thread = threading.Thread(
                    target=loop.run_forever, name="approval-broker", daemon=True
                )
                thread.start()
                self._loop, self._thread = loop, thread
            return self._loop  # type: ignore[return-value]  # None only once closed

    def close(self) -> None:
        """Deny every pending request, let the waiters return, then stop the private loop."""
        with self._lock:
            self._closed = True
            loop = self._loop
            for w in self._waiters.values():
                if w.decision is None:
                    w.decision = ApprovalDecision(
                        approved=False, decided_by="unavailable", note="approval service closed"
                    )
                if loop is not None:
                    loop.call_soon_threadsafe(w.event.set)
        if loop is None:
            return
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with self._lock:
                if not self._waiters:
                    break
            time.sleep(0.01)
        with self._lock:
            self._loop = None
        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=2)

    # ---- API side

    def for_job(self, job_id: str) -> JobApprover:
        return JobApprover(self, job_id)

    def pending(self, job_id: str) -> PendingAction | None:
        with self._lock:
            w = self._waiters.get(job_id)
            return w.action if w and w.decision is None else None

    def resolve(
        self, job_id: str, approve: bool, note: str | None = None, action_id: str | None = None
    ) -> PendingAction | None:
        """Record a decision. Returns the resolved action, or ``None`` if nothing matched."""
        with self._lock:
            w = self._waiters.get(job_id)
            if w is None or w.decision is not None:
                return None
            if action_id is not None and action_id != w.action.id:
                return None
            w.decision = ApprovalDecision(approved=approve, decided_by="human", note=note)
            loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(w.event.set)
        return w.action

    # ---- agent side

    def request(self, job_id: str, action: PendingAction) -> ApprovalDecision:
        timeout = action.timeout_s if action.timeout_s is not None else self.timeout_s
        action.timeout_s = timeout
        started = time.monotonic()
        loop = self._ensure_loop()
        # asyncio.Event binds to a loop on first wait, so it may be created on this thread.
        waiter = _Waiter(action=action, event=asyncio.Event())
        with self._lock:
            if self._closed:
                return ApprovalDecision(
                    approved=False, decided_by="unavailable", note="approval service closed"
                )
            self._waiters[job_id] = waiter

        async def _wait() -> bool:
            try:
                await asyncio.wait_for(waiter.event.wait(), timeout=timeout)
                return True
            except TimeoutError:
                return False

        try:
            # The extra margin only matters if the loop dies; wait_for enforces the timeout.
            asyncio.run_coroutine_threadsafe(_wait(), loop).result(timeout=timeout + 5)
        except concurrent.futures.TimeoutError:
            pass
        finally:
            with self._lock:
                decision = waiter.decision
                if decision is None:  # timed out (or broker closed): mark so late POSTs 409
                    decision = waiter.decision = ApprovalDecision(
                        approved=False, decided_by="timeout"
                    )
                if self._waiters.get(job_id) is waiter:
                    del self._waiters[job_id]
        decision.waited_s = round(time.monotonic() - started, 3)
        return decision


@dataclass
class JobApprover:
    """:class:`Approver` bound to one API job."""

    broker: ApprovalBroker
    job_id: str

    def request(self, action: PendingAction) -> ApprovalDecision:
        return self.broker.request(self.job_id, action)


__all__ = [
    "ApprovalBroker",
    "ApprovalDecision",
    "ApprovalPolicy",
    "ApprovalRecord",
    "Approver",
    "AutoApprover",
    "CallbackApprover",
    "JobApprover",
    "PendingAction",
    "denial_message",
    "estimate_rows",
]
