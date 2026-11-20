from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from analyst_agent.core.llm import Usage, estimate_cost_usd
from analyst_agent.eval.experiments import (
    AGENT_PROFILE,
    CONTROL,
    CRITIC_PROFILE,
    HAIKU,
    MODEL_PROFILES,
    PLAN,
    SONNET,
    agent_profile,
    app,
    critic_profile,
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
    # agent cost is each model's own per-task profile at its prices (Haiku 5.5 has its own
    # calibrated profile; Sonnet 5.5 uses the default one)
    for arm, model in ((CONTROL, SONNET), ("haiku", HAIKU)):
        expected = sum(
            n * estimate_cost_usd(model, agent_profile(model, level), single_request=False)
            for level, n in COUNTS.items()
        )
        assert cost[arm].agent_usd == pytest.approx(expected)
    assert cost["haiku"].agent_usd < cost[CONTROL].agent_usd


def test_models_without_a_profile_use_the_default_one() -> None:
    assert agent_profile(SONNET, "L3") == AGENT_PROFILE["L3"]
    assert critic_profile(SONNET, "L3") == CRITIC_PROFILE["L3"]
    assert agent_profile("claude-mystery-9", "L1") == AGENT_PROFILE["L1"]
    # Haiku's own (smoke-calibrated) profile: about twice Sonnet's tool calls on L3
    haiku, haiku_critic = MODEL_PROFILES[HAIKU]
    assert agent_profile(HAIKU, "L3") == haiku["L3"] != AGENT_PROFILE["L3"]
    assert critic_profile(HAIKU, "L2") == haiku_critic["L2"]
    assert set(haiku) == set(haiku_critic) == set(AGENT_PROFILE)


def test_profiles_reproduce_the_calibration_smoke_run() -> None:
    """The 2026-11-06 smoke run (2 tasks per level, batched judge) spent $0.0952 on Haiku and
    $0.4152 on Sonnet; the calibrated profiles must re-estimate it within 5%."""
    smoke = {"L1": 2, "L2": 2, "L3": 2, "ADV": 2}
    for env, spent in (({"AGENT_MODEL": HAIKU}, 0.0952), ({}, 0.4152)):
        est = estimate_run(settings_for(env), smoke, batch_judge=True)
        assert est.total_usd == pytest.approx(spent, rel=0.05)


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
    # Every PLAN arm except cache, which repeats the control config and runs only when asked.
    assert len(cmds) == sum(len(arms) for exp, _, arms in PLAN if exp != "cache")
    assert all("--budget-usd" in c and "--judge-model claude-sonnet-5-5" in c for c in cmds)


def test_script_refuses_core_without_a_key() -> None:
    env = {**os.environ, "PYTHON": sys.executable}
    env.pop("ANTHROPIC_API_KEY", None)
    out = subprocess.run(
        ["bash", str(SCRIPT), "--yes"], capture_output=True, text=True, env=env, cwd=REPO
    )
    assert out.returncode == 2 and "ANTHROPIC_API_KEY is not set" in out.stderr


def _fake_darwin_bin(tmp_path: Path) -> Path:
    """``uname`` that says Darwin and a ``caffeinate`` that logs its args and runs the command."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "uname").write_text("#!/bin/sh\necho Darwin\n")
    (bin_dir / "caffeinate").write_text(
        f'#!/bin/sh\necho "$@" >> {tmp_path / "caffeinate.log"}\nshift\nexec "$@"\n'
    )
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    return bin_dir


def test_script_keeps_a_mac_awake_for_real_runs_only(tmp_path: Path) -> None:
    """The 2026-11-14 run lost 8 tasks to a laptop idle sleep: wrap runs in caffeinate -i."""
    env = {**os.environ, "PYTHON": sys.executable}
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("NO_CAFFEINATE", None)
    env.pop("RUN_EXPERIMENTS_CAFFEINATED", None)
    env["PATH"] = f"{_fake_darwin_bin(tmp_path)}{os.pathsep}{env['PATH']}"
    log = tmp_path / "caffeinate.log"

    out = subprocess.run(
        ["bash", str(SCRIPT), "--yes"], capture_output=True, text=True, env=env, cwd=REPO
    )
    assert out.returncode == 2 and "ANTHROPIC_API_KEY is not set" in out.stderr  # ran inside
    assert "caffeinate -i" in out.stderr
    (line,) = log.read_text().splitlines()  # wrapped exactly once, not re-exec'd in a loop
    assert line.startswith("-i bash ") and line.endswith(" --yes")

    log.unlink()
    for args in (["--help"], ["--agent", "dry-run", "--print-commands"]):
        subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, env=env, cwd=REPO)
    subprocess.run(
        ["bash", str(SCRIPT), "--yes"],
        capture_output=True,
        env={**env, "NO_CAFFEINATE": "1"},
        cwd=REPO,
    )
    assert not log.exists()


def test_usage_profiles_are_positive() -> None:
    for u in AGENT_PROFILE.values():
        assert isinstance(u, Usage) and u.total > 0


# ---- coverage of the remaining summary columns, notes and CLI paths --------------------------


def _routing(tier: str, escalations: list[dict] | None = None) -> dict:
    return {
        "enabled": True,
        "routed_tier": tier,
        "routed_model": "m",
        "final_tier": tier,
        "final_model": "m",
        "route": {},
        "escalations": escalations or [],
        "turn_models": [],
    }


def _manifest(tmp_path: Path, rows: list[tuple[str, str, str, Path]]) -> Path:
    path = tmp_path / "manifest.tsv"
    path.write_text(
        "experiment\tarm\trole\trun_dir\tconfig\n"
        + "".join(f"{e}\t{a}\t{r}\t{d}\tcfg\n" for e, a, r, d in rows)
    )
    return path


def test_summary_routing_structured_and_baseline_columns(tmp_path: Path) -> None:
    control = _write_run(
        tmp_path / "control", [_row("a", "L1", True, 0.2), _row("b", "L3", False, 0.4)]
    )
    esc = [{"from_tier": "L1", "to_tier": "L2", "trigger": "tool_error"}]
    routed = _write_run(
        tmp_path / "routed",
        [
            _row("a", "L1", True, 0.05, routing=_routing("L1", esc)),
            _row("b", "L3", True, 0.3, routing=_routing("L3")),
        ],
    )
    structured = _write_run(tmp_path / "structured", [_row("a", "L1", True, 0.2)])
    haiku = _write_run(tmp_path / "haiku", [_row("a", "L1", False, 0.01)])
    manifest = _manifest(
        tmp_path,
        [
            ("baseline", "haiku", "treatment", haiku),
            ("baseline", CONTROL, "control", control),
            ("routing", "routing-on", "treatment", routed),
            ("routing", CONTROL, "control", control),
            ("structured", "structured-on", "treatment", structured),
            ("structured", CONTROL, "control", control),
            ("custom", "x", "treatment", haiku),  # unknown experiment: title is its name
        ],
    )
    md = render_summary(read_manifest(manifest), {"agent": "`core`"})
    assert "- agent: `core`" in md
    baseline = md.split("### Baseline")[1].split("###")[0]
    assert "Mean tool calls" in baseline and "| 3.0 |" in baseline
    routing = md.split("### Model routing")[1].split("###")[0]
    assert "| L1:1 / L3:1 | 50.0% |" in routing  # tiers, escalation rate
    assert "| – | – |" in routing  # the control was not routed
    structured_tbl = md.split("### Structured")[1].split("###")[0]
    assert "Numeric text-fallback" in structured_tbl
    assert "### custom" in md


def test_arm_notes_cover_errors_timeouts_missing_judge_and_ungraded(tmp_path: Path) -> None:
    rows = [
        _row("a", "L1", False, 0.0, status="error"),
        _row("b", "L2", False, 0.0, status="timeout"),
        _row("c", "L3", False, 0.1, score_details={"method": "rubric", "skipped": True}),
    ]
    judged = _write_run(tmp_path / "judged", rows)
    unjudged = _write_run(tmp_path / "unjudged", rows)
    meta = json.loads((unjudged / "meta.json").read_text())
    meta["config"] = {}
    (unjudged / "meta.json").write_text(json.dumps(meta))
    manifest = _manifest(
        tmp_path,
        [("critic", CONTROL, "control", judged), ("critic", "critic-off", "treatment", unjudged)],
    )
    md = render_summary(read_manifest(manifest))
    assert "1 err / 1 timeout" in md
    assert "sonnet: 1 err / 1 timeout; 1 ungraded" in md
    assert "critic-off: 1 err / 1 timeout; no judge: L3 skipped" in md


def test_arm_notes_warn_when_the_host_slept(tmp_path: Path) -> None:
    rows = [_row("a", "L1", True, 1.0), _row("b", "L3", False, 0.0, status="timeout")]
    slept = _write_run(tmp_path / "slept", rows)
    meta = json.loads((slept / "meta.json").read_text())
    meta["host_sleep"] = [
        {"after": "start a", "before": "start b", "suspended_s": 1800.0},
        {"after": "start b", "before": "end of run", "suspended_s": 600.0},
    ]
    (slept / "meta.json").write_text(json.dumps(meta))
    ok = _write_run(tmp_path / "ok", rows)
    manifest = _manifest(
        tmp_path,
        [("semantic", CONTROL, "control", ok), ("semantic", "semantic-bm25", "treatment", slept)],
    )
    md = render_summary(read_manifest(manifest))
    assert "semantic-bm25: 0 err / 1 timeout; WARNING: host slept 2x (~40 min)" in md
    assert md.count("host slept") == 2  # the overview row and the semantic notes, not sonnet


def test_formatting_helpers_handle_missing_values() -> None:
    from analyst_agent.eval import experiments as ex

    assert ex._pct(None) == ex._pp(None, 0.5) == ex._usd(None) == "–"
    assert ex._ratio(0.0, 1.0) == ex._ratio(1.0, None) == "–"
    assert ex._usd(0.05) == "$0.0500" and ex._usd(1.5) == "$1.500"
    assert ex._scaled(Usage(10, 20, 30, 40), 0.5) == Usage(5, 10, 15, 20)


def test_estimate_skips_empty_levels_and_counts_routing_and_extras() -> None:
    base = settings_for({})
    est = estimate_run(base, {"L1": 2, "L2": 0})
    assert set(est.by_level) == {"L1"} and est.router_usd == 0
    routed = estimate_run(
        settings_for(
            {
                "AGENT_ROUTING_ENABLED": "true",
                "AGENT_MODEL_L1": HAIKU,
                "AGENT_SEMANTIC_LAYER_ENABLED": "true",
                "AGENT_STRUCTURED_REPORT_ENABLED": "true",
            }
        ),
        {"L1": 2},
        judge=False,
    )
    assert routed.router_usd > 0 and routed.judge_usd == 0
    assert routed.models["judge"] == "off" and routed.models["router"]


def test_estimate_cli_overrides_and_bad_format(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    for key in ("AGENT_ROUTING_ENABLED", "AGENT_SEMANTIC_LAYER_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    plain = json.loads(runner.invoke(app, ["estimate", "--limit", "4", "--no-judge"]).output)
    assert plain["judge_usd"] == 0 and plain["router_usd"] == 0
    routed = json.loads(
        runner.invoke(app, ["estimate", "--limit", "4", "--routing", "--semantic-layer"]).output
    )
    assert routed["router_usd"] > 0 and routed["agent_usd"] > plain["agent_usd"]
    bad = runner.invoke(app, ["estimate", "--limit", "4", "--format", "xml"])
    assert bad.exit_code != 0 and "usd or json" in bad.output


def test_plan_and_summary_commands(tmp_path: Path) -> None:
    runner = CliRunner()
    out = runner.invoke(app, ["plan", "--cap-factor", "2"])
    assert out.exit_code == 0 and "Cap (×2)" in out.output and "| **all** |" in out.output

    empty = tmp_path / "empty.tsv"
    empty.write_text("experiment\tarm\trole\trun_dir\tconfig\n")
    res = runner.invoke(app, ["summary", str(empty)])
    # Rich wraps the error panel at the terminal width, and pytest's tmp path is long enough
    # to split the message across lines: compare with the panel's borders and breaks removed.
    flat = " ".join(res.output.replace("│", " ").split())
    assert res.exit_code != 0 and "lists no runs" in flat

    run = _write_run(tmp_path / "run", [_row("a", "L1", True, 0.1)])
    manifest = _manifest(tmp_path, [("baseline", CONTROL, "control", run)])
    dest = tmp_path / "out" / "summary.md"
    res = runner.invoke(app, ["summary", str(manifest), "--out", str(dest), "--agent", "core"])
    assert res.exit_code == 0, res.output
    text = dest.read_text()
    assert "- agent: `core`" in text and "- manifest:" in text and text in res.output


def test_module_entry_point_prints_help() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "analyst_agent.eval.experiments", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0 and "estimate" in out.stdout and "summary" in out.stdout


def test_batch_judge_halves_the_judge_estimate() -> None:
    s = settings_for({})
    direct, batched = estimate_run(s, COUNTS), estimate_run(s, COUNTS, batch_judge=True)
    assert batched.judge_usd == pytest.approx(direct.judge_usd / 2)
    assert batched.agent_usd == direct.agent_usd
    res = CliRunner().invoke(app, ["estimate", "--format", "usd", "--batch"])
    assert res.exit_code == 0 and float(res.output) > 0


def test_summary_merges_manifests(tmp_path: Path) -> None:
    control = _write_run(tmp_path / "control", [_row("a", "L1", True, 0.1)])
    haiku = _write_run(tmp_path / "haiku", [_row("a", "L1", False, 0.01)])
    off = _write_run(tmp_path / "off", [_row("a", "L1", True, 0.05, critic_rounds=0)])
    first = tmp_path / "m1.tsv"
    first.write_text(
        "experiment\tarm\trole\trun_dir\tconfig\n"
        f"baseline\thaiku\ttreatment\t{haiku}\tHaiku\n"
        f"baseline\tsonnet\tcontrol\t{control}\tSonnet\n"
    )
    second = tmp_path / "m2.tsv"
    second.write_text(
        "experiment\tarm\trole\trun_dir\tconfig\n"
        f"critic\tcritic-off\ttreatment\t{off}\tno critic\n"
        f"critic\tsonnet\tcontrol\t{control}\tSonnet\n"
    )
    out = tmp_path / "summary.md"
    res = CliRunner().invoke(app, ["summary", str(first), str(second), "--out", str(out)])
    assert res.exit_code == 0, res.output
    md = out.read_text()
    overview = md.split("## Overview")[1].split("###")[0]
    assert "| haiku |" in overview and "| critic-off |" in overview
    assert overview.count("| sonnet |") == 1


def test_script_batch_and_resume_commands(tmp_path: Path) -> None:
    env = {**os.environ, "PYTHON": sys.executable}
    out_dir = tmp_path / "inv"
    prev = out_dir / "critic-off" / "20260101T000000Z"
    prev.mkdir(parents=True)
    (prev / "meta.json").write_text("{}")
    out = subprocess.run(
        ["bash", str(SCRIPT), "--agent", "dry-run", "--print-commands", "--batch"]
        + ["--only", "critic", "--resume", str(out_dir)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
        cwd=REPO,
    )
    cmds = [ln for ln in out.stdout.splitlines() if "analyst_agent.eval run" in ln]
    by_arm = {("critic-off" if "--resume" in c else "sonnet"): c for c in cmds}
    assert f"--resume {prev}" in by_arm["critic-off"] and "--batch" in by_arm["critic-off"]
    assert "--limit" not in by_arm["critic-off"] and "--runs-dir" not in by_arm["critic-off"]
    assert f"--runs-dir {out_dir}/sonnet" in by_arm["sonnet"] and "--batch" in by_arm["sonnet"]
    assert f"(resume {prev})" in out.stdout
    bad = subprocess.run(
        ["bash", str(SCRIPT), "--agent", "dry-run", "--resume", str(tmp_path / "missing")],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO,
    )
    assert bad.returncode == 2 and "no such directory" in bad.stderr


# --------------------------------------------------------------------------- ADV-only PII arm


def test_plan_has_an_adv_only_pii_guard_arm() -> None:
    (arm,) = next(arms for exp, _, arms in PLAN if exp == "adv")
    assert arm.name == "pii-guard-on" and arm.difficulty == ("ADV",)
    assert settings_for(arm.env).pii_guard_enabled is True
    assert settings_for({}).pii_guard_enabled is False  # pinned off for every other arm
    assert arm.counts(COUNTS) == {"L1": 0, "L2": 0, "L3": 0, "ADV": 16}
    adv = estimate_run(settings_for(arm.env), arm.counts(COUNTS))
    full = estimate_run(settings_for(arm.env), COUNTS)
    assert adv.n_tasks == 16 and adv.total_usd < full.total_usd / 3


def test_judge_samples_multiply_only_the_l3_judge() -> None:
    s = settings_for({})
    one, three = estimate_run(s, COUNTS), estimate_run(s, COUNTS, judge_samples=3)
    l3_judge = estimate_cost_usd(SONNET, Usage(4_000, 740), single_request=False) * 16
    assert three.judge_usd == pytest.approx(one.judge_usd + 2 * l3_judge)
    assert three.agent_usd == one.agent_usd
    adv_only = {"L1": 0, "L2": 0, "L3": 0, "ADV": 16}
    assert estimate_run(s, adv_only, judge_samples=3).judge_usd == pytest.approx(
        estimate_run(s, adv_only).judge_usd
    )
    out = CliRunner().invoke(app, ["estimate", "--judge-samples", "3", "-d", "L3"])
    assert json.loads(out.output)["n_tasks"] == 16


def test_adv_only_arm_is_compared_with_the_controls_same_tasks(tmp_path: Path) -> None:
    pii_fail = {
        "method": "adversarial",
        "rules": {"passed": False, "failures": ["39 email addresses in output (max 1)"]},
    }
    control = _write_run(
        tmp_path / "control",
        [
            _row("l1", "L1", True, 0.1),
            _row("l3", "L3", True, 0.1),
            _row("adv_05", "ADV", False, 0.1, score_details=pii_fail),
            _row("adv_07", "ADV", True, 0.1),
        ],
    )
    guarded = _write_run(
        tmp_path / "guarded",
        [
            _row("adv_05", "ADV", True, 0.1, trace=[{"type": "pii_guard", "source": "run_sql"}]),
            _row("adv_07", "ADV", True, 0.1),
        ],
    )
    meta = json.loads((guarded / "meta.json").read_text())
    meta["config"]["filters"] = {"difficulty": ["ADV"], "limit": None}  # as eval run writes it
    (guarded / "meta.json").write_text(json.dumps(meta))
    manifest = _manifest(
        tmp_path,
        [
            ("adv", "pii-guard-on", "treatment", guarded),
            ("adv", CONTROL, "control", control),
            ("structured", CONTROL, "control", control),
        ],
    )
    md = render_summary(read_manifest(manifest))
    table = md.split("### PII output guard")[1].split("###")[0]
    lines = table.splitlines()
    ctrl = next(ln for ln in lines if "sonnet (control)" in ln)
    guard = next(ln for ln in lines if ln.startswith("| pii-guard-on"))
    # The control is cut to its 2 ADV tasks (1 of 2 passed), not its 4 tasks (3 of 4).
    assert "cfg (same 2 tasks)" in ctrl and "| 50.0% | – |" in ctrl
    assert "| 100.0% | +50.0 pp |" in guard
    assert "| 1 / 2 | 0 |" in guard and "| 0 / 2 | 1 |" in ctrl  # guard fired; PII failures
    assert "pii-guard-on: ADV tasks only" in table  # the notes line under the table
    overview = md.split("## Overview")[1].split("###")[0]
    assert "| 75.0% |" in next(ln for ln in overview.splitlines() if "| sonnet |" in ln)
    # A subset arm whose tasks already cover the control's leaves the control as it is.
    small = _write_run(tmp_path / "small", [_row("adv_05", "ADV", True, 0.1)])
    meta_small = {"git_sha": "abc123", "config": {"judge_model": "judge-x"}}
    (small / "meta.json").write_text(json.dumps(meta_small))
    (tmp_path / "sub").mkdir()
    sub = _manifest(
        tmp_path / "sub",
        [("adv", "pii-guard-on", "treatment", guarded), ("adv", CONTROL, "control", small)],
    )
    assert "(same" not in render_summary(read_manifest(sub))


def test_script_adv_only_arm_and_judge_samples(tmp_path: Path) -> None:
    env = {**os.environ, "PYTHON": sys.executable, "NO_CAFFEINATE": "1"}
    env.pop("ANTHROPIC_API_KEY", None)
    control = _write_run(tmp_path / "control", [_row("a", "L1", True, 0.1)])
    out = subprocess.run(
        ["bash", str(SCRIPT), "--agent", "dry-run", "--print-commands", "--only", "adv"]
        + ["--only", "critic", "--control", str(control), "--judge-samples", "3"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
        cwd=REPO,
    )
    cmds = [ln for ln in out.stdout.splitlines() if "analyst_agent.eval run" in ln]
    assert len(cmds) == 2  # the control is reused, not re-run
    adv = next(c for c in cmds if "pii-guard-on" in c)
    critic = next(c for c in cmds if "critic-off" in c)
    assert "--difficulty ADV" in adv and "--difficulty" not in critic
    assert all("--judge-samples 3" in c for c in cmds)
    envs = [ln for ln in out.stdout.splitlines() if ln.startswith("AGENT_MODEL=")]
    assert "AGENT_PII_GUARD_ENABLED=true" in envs[1] and "AGENT_PII_GUARD_ENABLED=false" in envs[0]
    bad = subprocess.run(
        ["bash", str(SCRIPT), "--agent", "dry-run", "--print-commands", "--judge-samples", "0"],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO,
    )
    assert bad.returncode == 2 and "--judge-samples" in bad.stderr
