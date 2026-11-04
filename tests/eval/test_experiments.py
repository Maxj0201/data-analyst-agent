from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from analyst_agent.core.llm import PRICING, Usage, estimate_cost_usd
from analyst_agent.eval.experiments import (
    AGENT_PROFILE,
    CONTROL,
    HAIKU,
    PLAN,
    SONNET,
    app,
    estimate_run,
    level_counts,
    plan_table,
    read_manifest,
    render_summary,
    settings_for,
)

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "run_experiments.sh"
COUNTS = {"L1": 24, "L2": 24, "L3": 16, "ADV": 16}


def test_benchmark_has_80_tasks() -> None:
    assert level_counts() == COUNTS


def test_settings_for_pins_every_flag_regardless_of_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_ROUTING_ENABLED", "true")
    monkeypatch.setenv("AGENT_MEMORY_ENABLED", "true")
    s = settings_for({})
    assert s.model == SONNET and s.judge_model == SONNET
    assert not s.routing_enabled and not s.memory_enabled
    assert s.critic_model is None and s.max_critic_rounds == 2


def test_estimate_uses_the_price_table() -> None:
    s = settings_for({"AGENT_MAX_CRITIC_ROUNDS": "0"})
    est = estimate_run(s, {"L1": 1}, judge=False)
    assert est.total_usd == pytest.approx(estimate_cost_usd(SONNET, AGENT_PROFILE["L1"]))
    assert est.critic_usd == 0 and est.models["critic"] == "off"


def test_estimate_orders_arms_sensibly() -> None:
    cost = {
        arm.name: estimate_run(settings_for(arm.env), COUNTS) for _, _, arms in PLAN for arm in arms
    }
    assert cost["haiku"].total_usd < cost["routing-on"].total_usd < cost[CONTROL].total_usd
    assert cost["critic-off"].total_usd < cost[CONTROL].total_usd
    assert cost["critic-off"].critic_usd == 0
    assert cost["semantic-bm25"].total_usd > cost[CONTROL].total_usd
    assert cost["structured-on"].total_usd > cost[CONTROL].total_usd
    assert cost["routing-on"].router_usd > 0 and cost[CONTROL].router_usd == 0
    # the judge is the same model in every arm, so it costs the same everywhere
    assert len({round(c.judge_usd, 6) for c in cost.values()}) == 1
    # haiku agent cost scales with the price ratio
    ratio = PRICING[SONNET][0] / PRICING[HAIKU][0]
    assert cost[CONTROL].agent_usd == pytest.approx(cost["haiku"].agent_usd * ratio)


def test_estimate_flags_unpriced_models() -> None:
    est = estimate_run(settings_for({"AGENT_MODEL": "claude-mystery-9"}), {"L1": 1})
    assert est.models["unpriced"] == "claude-mystery-9"


def test_plan_table_lists_every_arm_and_a_total() -> None:
    md = plan_table(COUNTS)
    for _, _, arms in PLAN:
        for arm in arms:
            assert f"| {arm.name} |" in md
    assert "| **all** |" in md


def test_estimate_cli_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    monkeypatch.setenv("AGENT_MODEL", HAIKU)
    monkeypatch.setenv("AGENT_JUDGE_MODEL", HAIKU)
    cheap = float(runner.invoke(app, ["estimate", "--format", "usd"]).output)
    monkeypatch.setenv("AGENT_MODEL", SONNET)
    dear = runner.invoke(app, ["estimate", "--format", "json", "--factor", "2"])
    data = json.loads(dear.output)
    assert data["n_tasks"] == 80 and data["factor"] == 2
    assert cheap < data["total_usd"]
    limited = json.loads(runner.invoke(app, ["estimate", "--limit", "8"]).output)
    assert limited["n_tasks"] == 8


def _write_run(d: Path, results: list[dict], judge_usd: float = 0.0) -> Path:
    d.mkdir(parents=True)
    with open(d / "results.jsonl", "w") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")
    meta = {"git_sha": "abc123", "config": {"judge_model": "judge-x"}}
    meta["judge_cost"] = {"usd": judge_usd}
    (d / "meta.json").write_text(json.dumps(meta))
    return d


def _row(task: str, level: str, passed: bool, cost: float, **kw: object) -> dict:
    return {
        "task_id": task,
        "difficulty": level,
        "tags": [],
        "status": kw.pop("status", "ok"),
        "passed": passed,
        "score": float(passed),
        "score_details": kw.pop("score_details", {"method": "numeric"}),
        "cost_usd": cost,
        "latency_s": 2.0,
        "tool_calls": 3,
        "critic_rounds": kw.pop("critic_rounds", 1),
        "critic_revisions": 0,
        "trace": kw.pop("trace", []),
        **kw,
    }


def test_summary_tables(tmp_path: Path) -> None:
    control = _write_run(
        tmp_path / "control",
        [_row("a", "L1", True, 0.1), _row("b", "L2", False, 0.1)],
        judge_usd=0.05,
    )
    off = _write_run(
        tmp_path / "off",
        [
            _row("a", "L1", True, 0.05, critic_rounds=0),
            _row("b", "L2", True, 0.05, critic_rounds=0),
        ],
    )
    sem = _write_run(
        tmp_path / "sem",
        [
            _row("a", "L1", True, 0.1, trace=[{"type": "semantic_retrieval", "metrics": ["m"]}]),
            _row(
                "b",
                "L2",
                False,
                0.0,
                status="over_budget",
                score_details={"method": "not_run", "skipped": True},
                trace=[{"type": "semantic_retrieval", "metrics": [], "error": "ImportError"}],
            ),
        ],
    )
    manifest = tmp_path / "manifest.tsv"
    manifest.write_text(
        "experiment\tarm\trole\trun_dir\tconfig\n"
        f"critic\tcritic-off\ttreatment\t{off}\tno critic\n"
        f"critic\tsonnet\tcontrol\t{control}\tSonnet\n"
        f"semantic\tsemantic-bm25\ttreatment\t{sem}\tbm25\n"
        f"semantic\tsonnet\tcontrol\t{control}\tSonnet\n"
    )
    md = render_summary(read_manifest(manifest))
    assert "## Overview" in md and "### Critic on vs off (Sonnet)" in md
    # the shared control appears once in the overview, first in each experiment table
    assert md.split("## Overview")[1].split("###")[0].count("| sonnet |") == 1
    critic = md.split("### Critic")[1].split("###")[0]
    assert critic.index("sonnet (control)") < critic.index("critic-off")
    assert "| 100.0% | +50.0 pp |" in critic  # 2/2 vs 1/2
    assert "| 0.50× |" in critic  # half the cost per task
    sem_table = md.split("### Semantic")[1]
    assert "1 not started (budget)" in sem_table and "1 retrieval errors" in sem_table
    assert "judge: `judge-x`" in md
    overview = md.split("## Overview")[1].split("###")[0].splitlines()
    control_row = next(ln for ln in overview if "| sonnet |" in ln)
    assert "| $0.250 |" in control_row  # agent $0.20 + judge $0.05


def test_script_help_and_print_commands() -> None:
    env = {**os.environ, "PYTHON": sys.executable}
    env.pop("ANTHROPIC_API_KEY", None)
    out = subprocess.run(
        ["bash", str(SCRIPT), "--help"], capture_output=True, text=True, env=env, check=True
    )
    assert "--budget-usd" in out.stdout
    out = subprocess.run(
        ["bash", str(SCRIPT), "--agent", "dry-run", "--print-commands", "--out", "runs/x"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
        cwd=REPO,
    )
    cmds = [ln for ln in out.stdout.splitlines() if "analyst_agent.eval run" in ln]
    assert len(cmds) == sum(len(arms) for _, _, arms in PLAN)
    assert all("--budget-usd" in c and "--judge-model claude-sonnet-5-5" in c for c in cmds)


def test_script_refuses_core_without_a_key() -> None:
    env = {**os.environ, "PYTHON": sys.executable}
    env.pop("ANTHROPIC_API_KEY", None)
    out = subprocess.run(
        ["bash", str(SCRIPT), "--yes"], capture_output=True, text=True, env=env, cwd=REPO
    )
    assert out.returncode == 2 and "ANTHROPIC_API_KEY is not set" in out.stderr


def test_usage_profiles_are_positive() -> None:
    for u in AGENT_PROFILE.values():
        assert isinstance(u, Usage) and u.total > 0
