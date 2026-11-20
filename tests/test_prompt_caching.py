"""Prompt caching: where the breakpoints go, that the cached prefix is byte-stable from turn to
turn, and the cache-hit-rate column of the eval reports (mocked usage counts, no API)."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from analyst_agent.core.config import Settings
from analyst_agent.core.llm import (
    EPHEMERAL,
    MAX_BREAKPOINTS,
    LLMClient,
    cache_conversation_tail,
    cached_text,
)
from analyst_agent.core.loop import AgentLoop, RunStatus
from analyst_agent.eval.experiments import read_manifest, render_summary
from analyst_agent.eval.report import cache_hit_rate, cache_tokens, render_report, summarize
from analyst_agent.tools import build_default_registry
from tests.conftest import FakeLLM, critic_json, resp, text, tool_use

SQL = "SELECT region, SUM(revenue) AS rev FROM sales GROUP BY region ORDER BY region"


def _marked(blocks: Any) -> list[int]:
    return [i for i, b in enumerate(blocks) if isinstance(b, dict) and "cache_control" in b]


def _breakpoints(params: dict[str, Any]) -> list[tuple[str, int, int]]:
    """Every cache_control in a request, as (where, message index, block index)."""
    out = [("system", -1, i) for i in _marked(params["system"])]
    for m, msg in enumerate(params["messages"]):
        if isinstance(msg["content"], list):
            out += [("messages", m, i) for i in _marked(msg["content"])]
    return out


def _strip(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The messages with cache markers removed and string content as one text block, i.e. what
    the cache key sees apart from where the breakpoints are."""
    out = []
    for msg in messages:
        content = msg["content"]
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        content = [{k: v for k, v in b.items() if k != "cache_control"} for b in content]
        out.append({**msg, "content": content})
    return out


# --------------------------------------------------------------------------- the tail breakpoint


def test_tail_breakpoint_goes_on_the_last_block_without_touching_the_history() -> None:
    history = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "run_sql"}]},
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "a"},
                {"type": "tool_result", "tool_use_id": "t2", "content": "b"},
            ],
        },
    ]
    before = deepcopy(history)
    out = cache_conversation_tail(history)
    assert history == before  # the loop's own list is never marked
    assert out[:2] == history[:2]
    assert out[-1]["content"][1]["cache_control"] == EPHEMERAL
    assert "cache_control" not in out[-1]["content"][0]


def test_tail_breakpoint_on_string_content_and_past_thinking_blocks() -> None:
    (msg,) = cache_conversation_tail([{"role": "user", "content": "Question: x"}])
    assert msg["content"] == [cached_text("Question: x")]

    thinking_last = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "plan"},
                {"type": "thinking", "thinking": "", "signature": "s"},
            ],
        },
    ]
    blocks = cache_conversation_tail(thinking_last)[-1]["content"]
    assert _marked(blocks) == [0]  # thinking blocks cannot carry cache_control

    only_thinking = [{"role": "assistant", "content": [{"type": "redacted_thinking"}]}]
    assert cache_conversation_tail(only_thinking) is only_thinking
    assert cache_conversation_tail([]) == []


def test_tail_breakpoint_respects_existing_markers_and_the_limit() -> None:
    router_msg = [{"role": "user", "content": [cached_text("tables")]}]
    assert cache_conversation_tail(router_msg) is router_msg  # already a breakpoint there

    parts = [cached_text(f"part {i}") for i in range(MAX_BREAKPOINTS - 1)]
    full = [{"role": "user", "content": [*parts, {"type": "text", "text": "tail"}]}]
    assert cache_conversation_tail(full) is full  # system + 3 = 4: no room for a fifth


def test_client_marks_the_tail_only_when_asked() -> None:
    client = LLMClient(Settings(_env_file=None, refusal_fallback=False), client=object())
    history = [{"role": "user", "content": "q"}]
    one_off = client._build_params("SYS", history, None, None, "claude-sonnet-5-5")
    assert _breakpoints(one_off) == [("system", -1, 0)]  # critic, judge, report writer
    multi = client._build_params(
        "SYS", history, [{"name": "t"}], None, "claude-sonnet-5-5", cache_history=True
    )
    assert _breakpoints(multi) == [("system", -1, 0), ("messages", 0, 0)]
    assert history == [{"role": "user", "content": "q"}]


# --------------------------------------------------------------------------- the agent loop


@pytest.fixture
def run(ctx, settings):  # noqa: ANN001, ANN201
    llm = FakeLLM(
        agent=[
            resp(text("<plan>1. inspect\n2. sum</plan>"), tool_use("inspect_schema")),
            resp(tool_use("run_sql", query=SQL)),
            resp(text("Not done yet.")),  # a text-only turn: the loop appends a nudge
            resp(
                tool_use(
                    "final_report",
                    markdown="# Revenue\nnorth 150, south 200",
                    key_numbers={"north": 150, "south": 200},
                )
            ),
        ],
        critic=[critic_json("pass")],
    )
    result = AgentLoop(llm, build_default_registry(), ctx, settings).run("Revenue by region?")
    assert result.status is RunStatus.COMPLETED
    client = LLMClient(settings, client=object())

    def params(call: dict[str, Any]) -> dict[str, Any]:
        return client._build_params(
            call["system"],
            call["messages"],
            call["tools"],
            call["output_format"],
            call["model"] or settings.model,
            effort=call["effort"],
            max_tokens=call["max_tokens"],
            cache_history=call["cache_history"],
        )

    return result, llm, [params(c) for c in llm.calls_for("agent")], params


def test_agent_turns_cache_tools_system_and_the_conversation(run) -> None:  # noqa: ANN001
    _, llm, reqs, _ = run
    assert len(reqs) == 4
    assert all(c["cache_history"] for c in llm.calls_for("agent"))
    for req in reqs:
        # 1: tools + system; 2: the conversation up to its newest block.
        n = len(req["messages"])
        last = len(req["messages"][-1]["content"]) - 1
        assert _breakpoints(req) == [("system", -1, 0), ("messages", n - 1, last)]
        assert len(_breakpoints(req)) <= MAX_BREAKPOINTS


def test_the_cached_prefix_does_not_change_between_turns(run) -> None:  # noqa: ANN001
    result, _, reqs, _ = run
    # tools and system: byte-identical on every turn, and free of per-run values.
    head = {json.dumps([r["tools"], r["system"]], sort_keys=False) for r in reqs}
    assert len(head) == 1
    (blob,) = head
    assert result.state.run_id and result.state.run_id not in blob
    for pattern in ("2026-", "T0", "run_id"):
        assert pattern not in blob
    # messages: each request's conversation is a prefix of the next one (append-only), so the
    # block the previous request marked is still there, at the same position, to be read.
    for prev, nxt in zip(reqs, reqs[1:], strict=False):
        a, b = _strip(prev["messages"]), _strip(nxt["messages"])
        assert b[: len(a)] == a
        (_, m, i) = _breakpoints(prev)[-1]
        assert b[m]["content"][i] == a[m]["content"][i]


def test_one_off_calls_do_not_mark_their_unique_input(run) -> None:  # noqa: ANN001
    _, llm, _, params = run
    (critic,) = llm.calls_for("critic")
    assert critic["cache_history"] is False
    req = params(critic)
    assert _breakpoints(req) == [("system", -1, 0)]  # CRITIC_SYSTEM, cached on its own


# --------------------------------------------------------------------------- eval reports


def _usage_row(task: str, read: int, write: int, uncached: int, **kw: Any) -> dict[str, Any]:
    return {
        "task_id": task,
        "difficulty": kw.pop("difficulty", "L1"),
        "tags": [],
        "status": "ok",
        "passed": True,
        "score": 1.0,
        "score_details": {"method": "numeric"},
        "cost_usd": 0.05,
        "latency_s": 2.0,
        "tool_calls": 3,
        "critic_rounds": 1,
        "critic_revisions": 0,
        "trace": [],
        "cost_by_model": {
            "claude-sonnet-5-5": {
                "calls": 4,
                "input_tokens": uncached,
                "cache_creation_input_tokens": write,
                "cache_read_input_tokens": read,
                "output_tokens": 700,
                "usd": 0.05,
            }
        },
        **kw,
    }


def test_cache_hit_rate_is_reads_over_all_input_tokens() -> None:
    # The 2026-11-19 control's first task: 4,578 read, 3,232 written, 4,104 uncached.
    rows = [_usage_row("a", 4578, 3232, 4104), _usage_row("b", 6000, 0, 2000)]
    assert cache_tokens(rows) == {"read": 10578, "write": 3232, "uncached": 6104}
    assert cache_hit_rate(rows) == pytest.approx(10578 / 19914)
    assert summarize(rows)["cache_hit_rate"] == pytest.approx(0.5312, abs=1e-4)
    md = render_report(rows)
    assert "| Cache hit rate (agent input tokens) | 53.1% |" in md
    # No token counts (old rows, dry runs without usage): no rate rather than 0%.
    bare = [{**rows[0], "cost_by_model": {}}]
    assert cache_hit_rate(bare) is None
    assert "| Cache hit rate (agent input tokens) | – |" in render_report(bare)


def _write_run(d: Path, rows: list[dict[str, Any]]) -> Path:
    d.mkdir(parents=True)
    (d / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    meta = {"git_sha": "abc", "config": {"judge_model": "j"}, "judge_cost": {"usd": 0.0}}
    (d / "meta.json").write_text(json.dumps(meta))
    return d


def test_summary_shows_cache_hit_and_the_cache_experiment(tmp_path: Path) -> None:
    control = _write_run(
        tmp_path / "control", [_usage_row("a", 400, 10, 590), _usage_row("b", 400, 10, 590)]
    )
    cached = _write_run(
        tmp_path / "cached", [_usage_row("a", 900, 50, 50), _usage_row("b", 900, 50, 50)]
    )
    manifest = tmp_path / "manifest.tsv"
    manifest.write_text(
        "experiment\tarm\trole\trun_dir\tconfig\n"
        f"cache\tsonnet-cache\ttreatment\t{cached}\tcached\n"
        f"cache\tsonnet\tcontrol\t{control}\tSonnet\n"
    )
    md = render_summary(read_manifest(manifest))
    overview = md.split("## Overview")[1].split("###")[0]
    assert "| $/task | Cache hit |" in overview
    assert "| 40.0% |" in overview and "| 90.0% |" in overview
    table = md.split("### Prompt caching")[1]
    assert "| Cache hit | p50 / p95 latency |" in table
    row = next(ln for ln in table.splitlines() if ln.startswith("| sonnet-cache |"))
    assert "| 90.0% |" in row and "| 90% / 5% / 5% |" in row and "| 1,000 |" in row
