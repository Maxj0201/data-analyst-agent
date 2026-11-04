"""Integration-notes open items: ``eval run --semantic-layer``, judge-less L3 runs are skipped
(not failed), and the default metrics dir does not depend on the working directory."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import analyst_agent.core as core_pkg
from analyst_agent.core.config import Settings, default_metrics_dir
from analyst_agent.eval.agent import AgentInput, CoreAgent, load_agent
from analyst_agent.eval.cli import app
from analyst_agent.eval.judge import LLMJudge
from analyst_agent.eval.scorers import score_task
from tests.conftest import FakeLLM, critic_json, resp, tool_use
from tests.eval.conftest import make_task, out

# ---- --semantic-layer -----------------------------------------------------------------------


def test_load_agent_semantic_layer_override_wins_over_env(
    sales_csv: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AGENT_ARTIFACTS_DIR", str(tmp_path / "art"))
    monkeypatch.setenv("AGENT_SEMANTIC_LAYER_ENABLED", "false")
    fake = FakeLLM(
        agent=[resp(tool_use("final_report", markdown="Revenue is 350.", key_numbers={"t": 350}))],
        critic=[critic_json("pass")],
    )
    monkeypatch.setattr(core_pkg, "LLMClient", lambda settings: fake)

    agent = load_agent("core", semantic_layer_enabled=True)
    assert isinstance(agent, CoreAgent) and agent.semantic_layer_enabled is True
    out = asyncio.run(agent(AgentInput("t1", "Total revenue?", [sales_csv])))

    assert out.status == "completed", out.error
    assert any(e["type"] == "semantic_retrieval" for e in out.trace)
    first_user = fake.calls_for("agent")[0]["messages"][0]["content"]
    assert "Question: Total revenue?" in first_user


def test_eval_cli_accepts_semantic_layer_and_records_it(tmp_path: Path):
    res = CliRunner().invoke(
        app,
        ["run", "--agent", "oracle", "--no-judge", "-d", "L1", "--limit", "1",
         "--semantic-layer", "--runs-dir", str(tmp_path)],
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    [run_dir] = [p for p in tmp_path.iterdir() if p.is_dir()]
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["config"]["semantic_layer_enabled"] is True


# ---- judge-less L3 is skipped -------------------------------------------------------------


def test_llm_judge_without_credentials_skips_l3_and_adv_judge(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    judge = LLMJudge()
    l3 = asyncio.run(score_task(make_task("L3"), out(), judge))
    assert l3.details.get("skipped") is True and not l3.passed

    adv = asyncio.run(score_task(make_task("ADV"), out(), judge))
    assert adv.details["judge"].get("skipped") is True


def test_eval_cli_without_api_key_skips_l3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    res = CliRunner().invoke(
        app,
        ["run", "--agent", "oracle", "-d", "L3", "--limit", "2", "--runs-dir", str(tmp_path)],
    )
    assert res.exit_code == 0, res.output
    [run_dir] = [p for p in tmp_path.iterdir() if p.is_dir()]
    rows = [json.loads(x) for x in (run_dir / "results.jsonl").read_text().splitlines()]
    assert rows and all(r["score_details"].get("skipped") for r in rows)
    assert all("judge call failed" not in str(r["score_details"]) for r in rows)


# ---- metrics dir ----------------------------------------------------------------------------


def test_default_metrics_dir_does_not_depend_on_cwd(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("AGENT_SEMANTIC_METRICS_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    s = Settings(_env_file=None)
    assert s.semantic_metrics_dir is None
    resolved = s.resolved_semantic_metrics_dir
    assert resolved == default_metrics_dir() and resolved.is_absolute()
    assert sorted(p.name for p in resolved.glob("*.yaml")) == [
        "ecommerce.yaml",
        "hr.yaml",
        "saas.yaml",
    ]


def test_explicit_metrics_dir_is_kept(tmp_path: Path):
    s = Settings(_env_file=None, semantic_metrics_dir=tmp_path)
    assert s.resolved_semantic_metrics_dir == tmp_path


def test_default_metrics_dir_prefers_the_copy_bundled_in_the_wheel(tmp_path: Path, monkeypatch):
    from analyst_agent.core import config

    pkg = tmp_path / "site-packages" / "analyst_agent"
    (pkg / "semantic" / "metrics").mkdir(parents=True)
    monkeypatch.setattr(config, "_PACKAGE_DIR", pkg)
    assert config.default_metrics_dir() == pkg / "semantic" / "metrics"
