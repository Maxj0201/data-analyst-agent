"""Model routing: router classifier, tier -> model mapping and escalation inside the loop."""

from __future__ import annotations

import anthropic
import httpx2
import pytest

from analyst_agent.core.config import Settings
from analyst_agent.core.llm import Usage
from analyst_agent.core.loop import AgentLoop, RunStatus
from analyst_agent.core.prompts import ROUTER_SYSTEM
from analyst_agent.core.router import (
    ROUTER_SCHEMA,
    Router,
    Tier,
    next_tier,
    parse_route,
    schema_summary,
)
from analyst_agent.tools import build_default_registry
from tests.conftest import FakeLLM, critic_json, resp, text, tool_use

ROUTER = "claude-haiku-5-5"
M1, M2, M3 = "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1"
CRITIC = "claude-opus-5"  # distinct from every tier model so per-model counts are unambiguous
GOOD_SQL = "SELECT SUM(revenue) AS total FROM sales"
BAD_SQL = "SELECT nope FROM sales"


@pytest.fixture
def routed(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "routing_enabled": True,
            "router_model": ROUTER,
            "model_l1": M1,
            "model_l2": M2,
            "model_l3": M3,
            "critic_model": CRITIC,
        }
    )


def route(tier: str, reason: str = "r") -> object:
    return resp(text(f'{{"tier": "{tier}", "reason": "{reason}"}}'), model=ROUTER)


def final(model: str, answer: str = "Total is 350", **kn: object) -> object:
    return resp(
        tool_use("final_report", markdown=answer, key_numbers=kn or {"total": 350}), model=model
    )


def sql(model: str, query: str) -> object:
    return resp(tool_use("run_sql", query=query), model=model)


def make_loop(llm, ctx, settings, **kw):
    return AgentLoop(llm, build_default_registry(), ctx, settings, **kw)


def agent_models(llm: FakeLLM) -> list[str | None]:
    return [c["model"] for c in llm.calls_for("agent")]


# --------------------------------------------------------------------------- router unit


def test_parse_route():
    assert parse_route('{"tier": "L2", "reason": "group by"}') == (Tier.L2, "group by")
    assert parse_route('noise {"tier": "l3"} noise')[0] is Tier.L3
    with pytest.raises(ValueError):
        parse_route("not json")
    with pytest.raises(ValueError):
        parse_route('{"tier": "L4"}')
    with pytest.raises(ValueError):
        parse_route('["L1"]')


def test_next_tier_and_tier_models(settings: Settings, routed: Settings):
    assert [next_tier(t) for t in ("L1", "L2", "L3")] == [Tier.L2, Tier.L3, None]
    # Unset tiers fall back to AGENT_MODEL.
    assert {settings.model_for_tier(t) for t in ("L1", "L2", "L3")} == {settings.model}
    assert [routed.model_for_tier(t) for t in ("L1", "L2", "L3")] == [M1, M2, M3]
    with pytest.raises(ValueError):
        settings.model_for_tier("L9")


def test_routing_is_off_by_default():
    assert Settings(_env_file=None).routing_enabled is False


def test_schema_summary(ctx):
    s = schema_summary(ctx)
    assert s.startswith("sales (4 rows): ")
    assert "region VARCHAR" in s and "revenue" in s


def test_router_call_shape(ctx, routed):
    llm = FakeLLM(agent=[], router=[route("L2", "group by region")])
    decision = Router(llm, routed).classify("Revenue by region?", schema_summary(ctx))

    assert decision.tier is Tier.L2 and decision.model == M2
    assert decision.reason == "group by region" and decision.source == "router"
    call = llm.calls_for("router")[0]
    assert call["model"] == ROUTER
    assert call["effort"] == routed.router_effort == "low"
    assert call["max_tokens"] == routed.router_max_tokens
    assert call["system"] == ROUTER_SYSTEM
    assert call["output_format"] == {"type": "json_schema", "schema": ROUTER_SCHEMA}
    assert call["tools"] is None
    content = call["messages"][0]["content"]
    assert "Revenue by region?" in content
    # The schema summary is dataset content, so it is wrapped as untrusted data.
    assert '<tool_output tool="schema_summary"' in content and "sales (4 rows)" in content


@pytest.mark.parametrize(
    "outcome",
    [
        resp(text("I think it is medium"), model=ROUTER),
        resp(stop="refusal", model=ROUTER),
        anthropic.APIConnectionError(
            request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        ),
    ],
)
def test_router_failure_falls_back(routed, outcome):
    decision = Router(FakeLLM(agent=[], router=[outcome]), routed).classify("q", "")
    assert decision.source == "fallback" and decision.error
    assert decision.tier is Tier(routed.router_fallback_tier) is Tier.L3
    assert decision.model == M3


# --------------------------------------------------------------------------- loop: routing


def test_routing_disabled_keeps_default_model(ctx, settings):
    llm = FakeLLM(agent=[final(M2)], critic=[critic_json("pass")])  # no router responses
    result = make_loop(llm, ctx, settings).run("q")

    assert result.status is RunStatus.COMPLETED
    assert llm.calls_for("router") == []
    assert agent_models(llm) == [None]  # LLMClient then uses AGENT_MODEL
    assert result.state.routing_summary() is None
    assert result.to_dict()["routing"] is None


def test_routed_run_uses_tier_model_and_tracks_cost_per_model(ctx, routed):
    events: list[tuple[str, dict]] = []
    llm = FakeLLM(
        router=[route("L1", "single aggregate")],
        agent=[sql(M1, GOOD_SQL), final(M1)],
        critic=[critic_json("pass", model=CRITIC)],
    )
    result = make_loop(llm, ctx, routed, on_event=lambda k, p: events.append((k, p))).run(
        "Total revenue?"
    )

    assert result.status is RunStatus.COMPLETED
    st = result.state
    assert st.tier is Tier.L1 and st.model == M1
    assert agent_models(llm) == [M1, M1]
    assert st.turn_models == [M1, M1]
    assert [r.model for r in st.tool_results] == [M1, M1]
    assert llm.calls_for("critic")[0]["model"] == CRITIC  # the critic is not routed
    assert st.escalations == []

    # Router call happens first and is billed under the router model.
    assert llm.calls[0]["role"] == "router"
    assert set(st.cost.by_role) == {"router", "agent", "critic"}
    by_model = st.cost.to_dict()["by_model"]
    assert by_model[ROUTER]["calls"] == 1
    assert by_model[M1]["calls"] == 2
    assert by_model[CRITIC]["calls"] == 1

    summary = result.to_dict()["routing"]
    assert summary["routed_tier"] == "L1" and summary["final_tier"] == "L1"
    assert summary["routed_model"] == M1 and summary["escalations"] == []
    assert summary["route"]["reason"] == "single aggregate"
    route_events = [p for k, p in events if k == "route"]
    assert route_events == [st.route.to_dict()]


def test_router_failure_does_not_stop_the_run(ctx, routed):
    llm = FakeLLM(
        router=[resp(text("??"), model=ROUTER)],
        agent=[final(M3)],
        critic=[critic_json("pass")],
    )
    result = make_loop(llm, ctx, routed).run("q")
    assert result.status is RunStatus.COMPLETED
    assert result.state.route.source == "fallback"
    assert agent_models(llm) == [M3]


# --------------------------------------------------------------------------- loop: escalation


def test_second_critic_revise_escalates_and_retries_once(ctx, routed):
    events: list[tuple[str, dict]] = []
    llm = FakeLLM(
        router=[route("L1")],
        agent=[final(M1, "400"), final(M1, "390"), final(M2, "350")],
        critic=[
            critic_json("revise", ["400 is not supported"], model=CRITIC),
            critic_json("revise", ["390 is still wrong"], model=CRITIC),
            critic_json("pass", model=CRITIC),
        ],
    )
    result = make_loop(llm, ctx, routed, on_event=lambda k, p: events.append((k, p))).run("q")

    # Without routing this would stop after 2 rounds as completed_with_issues; the escalation
    # grants one extra review, and the stronger model's revision passes it.
    assert result.status is RunStatus.COMPLETED
    assert result.report == "350"
    st = result.state
    assert st.critic_rounds == 3 and st.critic_revisions == 2
    assert agent_models(llm) == [M1, M1, M2]
    assert st.tier is Tier.L2 and st.model == M2

    [esc] = st.escalations
    assert (esc.from_tier, esc.to_tier, esc.trigger) == (Tier.L1, Tier.L2, "critic_revise")
    assert (esc.from_model, esc.to_model) == (M1, M2)
    assert esc.turn == 2 and "390 is still wrong" in esc.detail
    assert [p["trigger"] for k, p in events if k == "escalation"] == ["critic_revise"]

    # The escalated model received the critic's feedback for the retry.
    last_msgs = llm.calls_for("agent")[2]["messages"]
    assert "390 is still wrong" in str(last_msgs[-1]["content"])

    by_model = st.cost.to_dict()["by_model"]
    assert {m: u["calls"] for m, u in by_model.items()} == {ROUTER: 1, M1: 2, M2: 1, CRITIC: 3}


def test_escalated_retry_failing_again_stops_with_issues(ctx, routed):
    llm = FakeLLM(
        router=[route("L1")],
        agent=[final(M1), final(M1), final(M2)],
        critic=[critic_json("revise", ["a"]), critic_json("revise", ["b"]), critic_json("revise")],
    )
    result = make_loop(llm, ctx, routed).run("q")
    assert result.status is RunStatus.COMPLETED_WITH_ISSUES
    assert result.state.critic_rounds == 3  # retried exactly once
    assert len(result.state.escalations) == 1  # only the *second* revise escalates


def test_no_escalation_at_top_tier(ctx, routed):
    llm = FakeLLM(
        router=[route("L3")],
        agent=[final(M3), final(M3)],
        critic=[critic_json("revise", ["a"]), critic_json("revise", ["b"])],
    )
    result = make_loop(llm, ctx, routed).run("q")
    assert result.status is RunStatus.COMPLETED_WITH_ISSUES
    assert result.state.critic_rounds == 2
    assert result.state.escalations == []
    assert agent_models(llm) == [M3, M3]


def test_repeated_tool_error_escalates(ctx, routed):
    llm = FakeLLM(
        router=[route("L1")],
        agent=[sql(M1, BAD_SQL), sql(M1, BAD_SQL), sql(M2, GOOD_SQL), final(M2)],
        critic=[critic_json("pass")],
    )
    result = make_loop(llm, ctx, routed).run("Total revenue?")

    assert result.status is RunStatus.COMPLETED
    st = result.state
    assert agent_models(llm) == [M1, M1, M2, M2]
    [esc] = st.escalations
    assert esc.trigger == "tool_error" and esc.turn == 2
    assert (esc.from_tier, esc.to_tier) == (Tier.L1, Tier.L2)
    assert "run_sql" in esc.detail
    assert [r.is_error for r in st.tool_results] == [True, True, False, False]
    assert [r.model for r in st.tool_results] == [M1, M1, M2, M2]
    # The stronger model sees both error results in its history.
    retry_history = str(llm.calls_for("agent")[2]["messages"])
    assert retry_history.count('status="error"') == 2


def test_non_consecutive_tool_errors_do_not_escalate(ctx, routed):
    llm = FakeLLM(
        router=[route("L1")],
        agent=[sql(M1, BAD_SQL), sql(M1, GOOD_SQL), sql(M1, BAD_SQL), final(M1)],
        critic=[critic_json("pass")],
    )
    result = make_loop(llm, ctx, routed).run("q")
    assert result.status is RunStatus.COMPLETED
    assert result.state.escalations == []
    assert agent_models(llm) == [M1] * 4


def test_tool_error_escalation_grants_one_extra_turn(ctx, routed):
    routed.max_turns = 2
    llm = FakeLLM(
        router=[route("L1")],
        agent=[sql(M1, BAD_SQL), sql(M1, BAD_SQL), final(M2)],
        critic=[critic_json("pass")],
    )
    result = make_loop(llm, ctx, routed).run("q")
    assert result.status is RunStatus.COMPLETED
    assert result.state.turns == 3 and result.state.extra_turns == 1


def test_escalations_chain_to_l3_and_are_all_recorded(ctx, routed):
    llm = FakeLLM(
        router=[route("L1")],
        agent=[
            sql(M1, BAD_SQL),
            sql(M1, BAD_SQL),  # -> L2 (tool_error)
            final(M2),
            final(M2),  # second revise -> L3 (critic_revise)
            final(M3),
        ],
        critic=[critic_json("revise", ["a"]), critic_json("revise", ["b"]), critic_json("pass")],
    )
    result = make_loop(llm, ctx, routed).run("q")

    assert result.status is RunStatus.COMPLETED
    st = result.state
    assert [(e.from_tier, e.to_tier, e.trigger) for e in st.escalations] == [
        (Tier.L1, Tier.L2, "tool_error"),
        (Tier.L2, Tier.L3, "critic_revise"),
    ]
    assert st.turn_models == [M1, M1, M2, M2, M3]
    summary = st.routing_summary()
    assert summary["routed_tier"] == "L1" and summary["final_tier"] == "L3"
    assert summary["final_model"] == M3
    assert [e["trigger"] for e in summary["escalations"]] == ["tool_error", "critic_revise"]
    assert set(st.cost.by_model) == {ROUTER, M1, M2, M3}


def test_escalation_is_a_noop_without_routing(ctx, settings):
    llm = FakeLLM(
        agent=[sql(M2, BAD_SQL), sql(M2, BAD_SQL), final(M2), final(M2)],
        critic=[critic_json("revise", ["a"]), critic_json("revise", ["b"])],
    )
    result = make_loop(llm, ctx, settings).run("q")
    assert result.status is RunStatus.COMPLETED_WITH_ISSUES
    assert result.state.escalations == [] and result.state.critic_rounds == 2
    assert agent_models(llm) == [None] * 4


def test_budget_still_applies_after_escalation(ctx, routed):
    routed.token_budget = 1_000
    big = Usage(input_tokens=500)  # router 150 + 2 x 500 >= 1_000
    llm = FakeLLM(
        router=[route("L1")],
        agent=[
            resp(tool_use("run_sql", query=BAD_SQL), model=M1, usage=big),
            resp(tool_use("run_sql", query=BAD_SQL), model=M1, usage=big),
            final(M2),  # never reached
        ],
    )
    result = make_loop(llm, ctx, routed).run("q")
    assert result.status is RunStatus.BUDGET_EXCEEDED
    assert len(result.state.escalations) == 1
    assert len(llm.queues["agent"]) == 1
