"""Human-in-the-loop approval: policy branches, approvers, the asyncio broker and loop wiring."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from analyst_agent.core.approval import (
    ApprovalBroker,
    ApprovalDecision,
    ApprovalPolicy,
    AutoApprover,
    CallbackApprover,
    PendingAction,
    denial_message,
    estimate_rows,
)
from analyst_agent.core.config import Settings
from analyst_agent.core.loop import AgentLoop, RunStatus
from analyst_agent.sandbox import SqlResult
from analyst_agent.tools import ToolContext, build_default_registry
from tests.conftest import FakeLLM, critic_json, resp, text, tool_use

SQL = "SELECT region, SUM(revenue) AS rev FROM sales GROUP BY region"
CODE = "print(df.shape)"

# --------------------------------------------------------------------------- helpers


@dataclass
class StubSandbox:
    """Answers EXPLAIN with a canned plan and records every query."""

    plan: str = "╭─ Seq Scan ─╮\n│ ~4,000 rows │\n╰────────────╯"
    explain_ok: bool = True
    queries: list[str] = field(default_factory=list)

    def run_sql(self, query: str) -> SqlResult:
        self.queries.append(query)
        if query.startswith("EXPLAIN"):
            if not self.explain_ok:
                return SqlResult(ok=False, error="ParserError: nope")
            return SqlResult(
                ok=True,
                rows=[{"explain_key": "physical_plan", "explain_value": self.plan}],
                columns=["explain_key", "explain_value"],
                row_count=1,
            )
        return SqlResult(ok=True, rows=[], columns=[], markdown_preview="(0 rows)")

    def run_python(self, code: str) -> Any:  # pragma: no cover - never reached by the policy
        raise AssertionError("policy must not execute python")


def stub_ctx(**kw: Any) -> tuple[ToolContext, StubSandbox]:
    sb = StubSandbox(**kw)
    return ToolContext(tables=["sales"], sandbox=sb), sb


def evaluate(policy: ApprovalPolicy, tool: str, args: dict[str, Any], ctx=None, **kw: Any):
    ctx = ctx or stub_ctx()[0]
    kw.setdefault("tokens_used", 0)
    kw.setdefault("token_budget", 1_000)
    return policy.evaluate(tool, args, ctx, **kw)


ON = dict(enabled=True, row_threshold=None, python=False, budget_fraction=None)

# --------------------------------------------------------------------------- settings


def test_settings_default_off(monkeypatch):
    for k in ("ENABLED", "ROW_THRESHOLD", "PYTHON", "BUDGET_FRACTION", "TIMEOUT_S"):
        monkeypatch.delenv(f"AGENT_APPROVAL_{k}", raising=False)
    s = Settings(_env_file=None)
    assert s.approval_enabled is False
    policy = ApprovalPolicy.from_settings(s)
    assert policy.enabled is False
    assert evaluate(policy, "run_python", {"code": CODE}, tokens_used=10**9) is None


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("AGENT_APPROVAL_ENABLED", "true")
    monkeypatch.setenv("AGENT_APPROVAL_ROW_THRESHOLD", "500")
    monkeypatch.setenv("AGENT_APPROVAL_PYTHON", "false")
    monkeypatch.setenv("AGENT_APPROVAL_BUDGET_FRACTION", "0.5")
    monkeypatch.setenv("AGENT_APPROVAL_TIMEOUT_S", "12.5")
    p = ApprovalPolicy.from_settings(Settings(_env_file=None))
    assert (p.enabled, p.row_threshold, p.python, p.budget_fraction, p.timeout_s) == (
        True,
        500,
        False,
        0.5,
        12.5,
    )


# --------------------------------------------------------------------------- (a) sql rows


@pytest.mark.parametrize(
    ("plan", "expected"),
    [
        ("│ ~273 rows │\n│ ~4,000 rows │\n│ ~600 rows │", 4000),
        ("│ ~1 row │", 1),
        ("EC: 1234\nEC: 99", 1234),
        ("│ Cross Product │", None),
    ],
)
def test_estimate_rows(plan, expected):
    assert estimate_rows(plan) == expected


def test_sql_over_threshold_needs_approval():
    ctx, sb = stub_ctx()
    action = evaluate(
        ApprovalPolicy(**{**ON, "row_threshold": 1_000}), "run_sql", {"query": SQL}, ctx
    )
    assert action is not None
    assert action.triggers == ["sql_rows"]
    assert action.estimated_rows == 4000
    assert "4,000 rows" in action.reasons[0] and "1,000" in action.reasons[0]
    assert SQL[:20] in action.summary
    assert sb.queries == [f"EXPLAIN {SQL}"]


def test_sql_at_or_under_threshold_runs_freely():
    ctx, _ = stub_ctx()
    policy = ApprovalPolicy(**{**ON, "row_threshold": 4_000})
    assert evaluate(policy, "run_sql", {"query": SQL + ";"}, ctx) is None


def test_sql_threshold_unset_skips_explain():
    ctx, sb = stub_ctx()
    assert evaluate(ApprovalPolicy(**ON), "run_sql", {"query": SQL}, ctx) is None
    assert sb.queries == []


def test_sql_explain_failure_is_not_gated():
    ctx, _ = stub_ctx(explain_ok=False)
    assert (
        evaluate(ApprovalPolicy(**{**ON, "row_threshold": 0}), "run_sql", {"query": "SELEC"}, ctx)
        is None
    )


def test_sql_that_is_already_explain_is_not_gated():
    ctx, sb = stub_ctx()
    policy = ApprovalPolicy(**{**ON, "row_threshold": 0})
    assert evaluate(policy, "run_sql", {"query": f"explain {SQL}"}, ctx) is None
    assert sb.queries == []


def test_sql_estimate_against_real_duckdb(ctx):
    # The 4-row sales fixture: DuckDB's own estimate drives the gate.
    assert ApprovalPolicy.sql_estimate("SELECT * FROM sales", ctx) == 4
    policy = ApprovalPolicy(**{**ON, "row_threshold": 3})
    assert evaluate(policy, "run_sql", {"query": "SELECT * FROM sales"}, ctx).estimated_rows == 4


# --------------------------------------------------------------------------- (b) python


def test_python_needs_approval():
    action = evaluate(ApprovalPolicy(**{**ON, "python": True}), "run_python", {"code": CODE})
    assert action is not None and action.triggers == ["python"]
    assert action.tool == "run_python" and action.input == {"code": CODE}


def test_python_gate_can_be_turned_off():
    assert evaluate(ApprovalPolicy(**ON), "run_python", {"code": CODE}) is None


def test_other_tools_not_gated_by_python_or_rows():
    policy = ApprovalPolicy(enabled=True, row_threshold=0, python=True, budget_fraction=None)
    assert evaluate(policy, "inspect_schema", {}) is None


# --------------------------------------------------------------------------- (c) budget


def test_budget_fraction_exceeded_gates_any_tool():
    policy = ApprovalPolicy(**{**ON, "budget_fraction": 0.5})
    action = evaluate(policy, "inspect_schema", {}, tokens_used=501, token_budget=1_000)
    assert action is not None and action.triggers == ["budget"]
    assert action.tokens_used == 501 and action.token_budget == 1_000
    assert "50%" in action.reasons[0]


def test_budget_fraction_not_exceeded():
    policy = ApprovalPolicy(**{**ON, "budget_fraction": 0.5})
    assert evaluate(policy, "inspect_schema", {}, tokens_used=500, token_budget=1_000) is None


def test_budget_already_approved_is_not_asked_again():
    policy = ApprovalPolicy(**{**ON, "budget_fraction": 0.5})
    kw = dict(tokens_used=900, token_budget=1_000, budget_approved=True)
    assert evaluate(policy, "inspect_schema", {}, **kw) is None


def test_final_report_is_never_gated():
    policy = ApprovalPolicy(enabled=True, row_threshold=0, python=True, budget_fraction=0.0)
    assert evaluate(policy, "final_report", {"markdown": "x"}, tokens_used=10**6) is None


def test_reasons_combine():
    policy = ApprovalPolicy(enabled=True, python=True, budget_fraction=0.1)
    action = evaluate(policy, "run_python", {"code": CODE}, tokens_used=200, token_budget=1_000)
    assert action.triggers == ["python", "budget"] and len(action.reasons) == 2


# --------------------------------------------------------------------------- approvers


def _action(timeout_s: float | None = 5.0) -> PendingAction:
    return PendingAction(
        id="appr_t",
        tool="run_python",
        input={"code": CODE},
        reasons=["r"],
        summary="s",
        triggers=["python"],
        timeout_s=timeout_s,
    )


def test_auto_approver():
    d = AutoApprover().request(_action())
    assert d.approved and d.decided_by == "auto"


@pytest.mark.parametrize(
    ("ret", "approved", "note"), [(True, True, None), ((False, "no"), False, "no")]
)
def test_callback_approver(ret, approved, note):
    d = CallbackApprover(lambda a: ret).request(_action())
    assert (d.approved, d.note, d.decided_by) == (approved, note, "human")


def test_denial_message_variants():
    a = _action(timeout_s=2)
    assert "no decision within 2s" in denial_message(a, ApprovalDecision(False, "timeout"))
    assert "no approver" in denial_message(a, ApprovalDecision(False, "unavailable"))
    msg = denial_message(a, ApprovalDecision(False, "human", note="use SQL"))
    assert "denied by the human reviewer" in msg and "Reviewer note: use SQL" in msg
    assert "Re-plan" in msg


# --------------------------------------------------------------------------- broker


def _request_in_thread(broker: ApprovalBroker, job_id: str, action: PendingAction):
    out: dict[str, ApprovalDecision] = {}
    t = threading.Thread(
        target=lambda: out.setdefault("d", broker.request(job_id, action)), daemon=True
    )
    t.start()
    deadline = time.monotonic() + 5
    while broker.pending(job_id) is None and t.is_alive():
        assert time.monotonic() < deadline, "request never registered"
        time.sleep(0.005)
    return t, out


def test_broker_resolve_approves_waiting_thread():
    broker = ApprovalBroker()
    try:
        t, out = _request_in_thread(broker, "job_1", _action())
        assert broker.pending("job_1").id == "appr_t"
        assert broker.pending("job_other") is None
        assert broker.resolve("job_1", True, note="ok") is not None
        t.join(5)
        d = out["d"]
        assert d.approved and d.decided_by == "human" and d.note == "ok"
        assert broker.pending("job_1") is None
        assert broker.resolve("job_1", True) is None  # already decided
    finally:
        broker.close()


def test_broker_action_id_guard():
    broker = ApprovalBroker()
    try:
        t, out = _request_in_thread(broker, "job_1", _action())
        assert broker.resolve("job_1", True, action_id="appr_stale") is None
        assert broker.resolve("job_1", False, action_id="appr_t") is not None
        t.join(5)
        assert out["d"].approved is False
    finally:
        broker.close()


def test_broker_timeout_is_denied():
    broker = ApprovalBroker()
    try:
        started = time.monotonic()
        d = broker.request("job_1", _action(timeout_s=0.1))
        assert time.monotonic() - started >= 0.09
        assert d.approved is False and d.decided_by == "timeout"
        assert d.waited_s >= 0.09
        assert broker.pending("job_1") is None
        assert broker.resolve("job_1", True) is None  # late decision is rejected
    finally:
        broker.close()


def test_broker_default_timeout_applies_when_action_has_none():
    broker = ApprovalBroker(timeout_s=0.05)
    try:
        action = _action(timeout_s=None)
        assert broker.request("j", action).decided_by == "timeout"
        assert action.timeout_s == 0.05
    finally:
        broker.close()


def test_broker_close_unblocks_waiters():
    broker = ApprovalBroker()
    t, out = _request_in_thread(broker, "job_1", _action(timeout_s=60))
    broker.close()
    t.join(5)
    assert not t.is_alive() and out["d"].approved is False


# --------------------------------------------------------------------------- loop wiring


def _loop(llm, ctx, settings, **kw):
    return AgentLoop(llm, build_default_registry(), ctx, settings, **kw)


def _python_then_report(note_check: list[str] | None = None) -> FakeLLM:
    return FakeLLM(
        agent=[
            resp(text("<plan>python</plan>"), tool_use("run_python", code="print(len(df))")),
            resp(tool_use("final_report", markdown="# Done", key_numbers={"n": 4})),
        ],
        critic=[critic_json("pass")],
    )


def _tool_results(message: dict[str, Any]) -> list[dict[str, Any]]:
    return [b for b in message["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]


@pytest.fixture
def gated_settings(settings):
    return settings.model_copy(
        update=dict(
            approval_enabled=True,
            approval_row_threshold=None,
            approval_python=True,
            approval_budget_fraction=None,
            approval_timeout_s=5.0,
        )
    )


def test_loop_disabled_by_default_never_asks(ctx, settings):
    asked: list[PendingAction] = []
    llm = _python_then_report()
    result = _loop(
        llm, ctx, settings, approver=CallbackApprover(lambda a: asked.append(a) or True)
    ).run("q")
    assert result.status is RunStatus.COMPLETED
    assert asked == [] and result.state.approvals == []


def test_loop_approved_step_executes_and_is_recorded(ctx, gated_settings):
    events: list[tuple[str, dict]] = []
    llm = _python_then_report()
    loop = _loop(
        llm,
        ctx,
        gated_settings,
        approver=CallbackApprover(lambda a: True),
        on_event=lambda k, p: events.append((k, p)),
    )
    result = loop.run("q")

    assert result.status is RunStatus.COMPLETED
    py = result.state.tool_results[0]
    assert py.name == "run_python" and not py.is_error and "4" in py.output
    kinds = [k for k, _ in events]
    i = kinds.index("approval_required")
    assert kinds[i - 1] == "tool_call" and kinds[i + 1] == "approval_decision"
    req = events[i][1]
    assert req["tool"] == "run_python" and req["triggers"] == ["python"] and req["timeout_s"] == 5.0
    assert events[i + 1][1]["approved"] is True and events[i + 1][1]["action_id"] == req["id"]

    (rec,) = result.state.approvals
    assert rec.action["id"] == req["id"] and rec.decision["approved"] is True
    trace = result.to_dict()
    assert trace["approvals"][0]["decision"]["decided_by"] == "human"


def test_loop_denied_step_is_tool_error_and_model_replans(ctx, gated_settings):
    llm = FakeLLM(
        agent=[
            resp(tool_use("run_python", code="print(1)")),
            resp(tool_use("run_sql", query=SQL)),  # re-plan with SQL
            resp(tool_use("final_report", markdown="# Done", key_numbers={})),
        ],
        critic=[critic_json("pass")],
    )
    result = _loop(
        llm, ctx, gated_settings, approver=CallbackApprover(lambda a: (False, "use SQL"))
    ).run("q")

    assert result.status is RunStatus.COMPLETED
    py, sql, _ = result.state.tool_results
    assert py.is_error and "Step not executed" in py.output and "use SQL" in py.output
    assert py.data["approval"]["approved"] is False
    assert not sql.is_error  # run_sql isn't gated by these settings
    # The model saw the denial as an is_error tool_result on its next turn.
    (block,) = _tool_results(llm.calls_for("agent")[1]["messages"][-1])
    assert block["is_error"] is True and "use SQL" in block["content"]
    assert result.state.approvals[0].decision["note"] == "use SQL"


def test_loop_without_approver_denies(ctx, gated_settings):
    result = _loop(_python_then_report(), ctx, gated_settings).run("q")
    py = result.state.tool_results[0]
    assert py.is_error and "no approver" in py.output
    assert result.state.approvals[0].decision["decided_by"] == "unavailable"


def test_loop_failing_approver_denies_without_crashing(ctx, gated_settings):
    def boom(_: PendingAction) -> bool:
        raise RuntimeError("tty gone")

    result = _loop(_python_then_report(), ctx, gated_settings, approver=CallbackApprover(boom)).run(
        "q"
    )
    assert result.status is RunStatus.COMPLETED
    assert result.state.tool_results[0].is_error
    assert "tty gone" in result.state.approvals[0].decision["note"]


def test_loop_timeout_via_broker_is_denied(ctx, gated_settings):
    broker = ApprovalBroker()
    try:
        s = gated_settings.model_copy(update={"approval_timeout_s": 0.1})
        result = _loop(_python_then_report(), ctx, s, approver=broker.for_job("j")).run("q")
    finally:
        broker.close()
    py = result.state.tool_results[0]
    assert py.is_error and "no decision within 0.1s" in py.output
    assert result.state.approvals[0].decision["decided_by"] == "timeout"


def test_loop_sql_row_gate(ctx, gated_settings):
    s = gated_settings.model_copy(update={"approval_python": False, "approval_row_threshold": 2})
    asked: list[PendingAction] = []
    llm = FakeLLM(
        agent=[
            resp(tool_use("run_sql", query="SELECT * FROM sales")),
            resp(tool_use("final_report", markdown="# Done", key_numbers={})),
        ],
        critic=[critic_json("pass")],
    )
    result = _loop(llm, ctx, s, approver=CallbackApprover(lambda a: asked.append(a) or True)).run(
        "q"
    )
    assert [a.triggers for a in asked] == [["sql_rows"]] and asked[0].estimated_rows == 4
    assert not result.state.tool_results[0].is_error


def test_loop_budget_gate_asked_once_after_approval(ctx, gated_settings):
    # Each scripted call costs 150 tokens; 10% of 1,000 is crossed after the first call.
    s = gated_settings.model_copy(
        update={"approval_python": False, "approval_budget_fraction": 0.1, "token_budget": 1_000}
    )
    asked: list[PendingAction] = []
    llm = FakeLLM(
        agent=[
            resp(tool_use("inspect_schema")),
            resp(tool_use("run_sql", query=SQL)),
            resp(tool_use("final_report", markdown="# Done", key_numbers={})),
        ],
        critic=[critic_json("pass")],
    )
    result = _loop(llm, ctx, s, approver=CallbackApprover(lambda a: asked.append(a) or True)).run(
        "q"
    )
    assert result.status is RunStatus.COMPLETED
    assert [a.tool for a in asked] == ["inspect_schema"] and asked[0].triggers == ["budget"]
    assert result.state.budget_approved is True


def test_loop_budget_gate_denied_is_asked_again(ctx, gated_settings):
    s = gated_settings.model_copy(
        update={"approval_python": False, "approval_budget_fraction": 0.1, "token_budget": 1_000}
    )
    asked: list[PendingAction] = []
    llm = FakeLLM(
        agent=[
            resp(tool_use("inspect_schema")),
            resp(tool_use("run_sql", query=SQL)),
            resp(tool_use("final_report", markdown="# Partial", key_numbers={})),
        ],
        critic=[critic_json("pass")],
    )
    result = _loop(llm, ctx, s, approver=CallbackApprover(lambda a: asked.append(a) or False)).run(
        "q"
    )
    assert [a.tool for a in asked] == ["inspect_schema", "run_sql"]
    assert all(r.is_error for r in result.state.tool_results[:2])
    assert result.report == "# Partial"
