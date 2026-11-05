"""scripts/preflight.sh / ``python -m analyst_agent.eval.preflight``, offline (FakeLLM)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from analyst_agent.core.config import Settings
from analyst_agent.core.fake_llm import FakeLLM
from analyst_agent.core.llm import LLMResponse, Usage
from analyst_agent.eval import preflight
from analyst_agent.eval.experiments import PLAN, estimate_run, level_counts, settings_for
from analyst_agent.eval.preflight import (
    Check,
    check_api_key,
    check_benchmark_regenerates,
    check_extras,
    check_metrics,
    check_models,
    check_writable,
    cost_table,
    dry_run_llm,
    embeddings_requested,
    extra_requirements,
    run_preflight,
    runbook_models,
)
from analyst_agent.eval.schema import DEFAULT_BENCHMARK_ROOT

REPO = Path(__file__).resolve().parents[2]
ALL_INSTALLED = {"api": [], "embeddings": [], "frontend": [], "mcp": []}


def test_runbook_models_cover_every_arm_judge_last() -> None:
    assert runbook_models() == ["claude-haiku-5-5", "claude-sonnet-5-5"]
    assert runbook_models("claude-opus-5-5")[-1] == "claude-opus-5-5"


def test_api_key_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert check_api_key({"ANTHROPIC_API_KEY": "sk"}).ok
    assert check_api_key({"ANTHROPIC_AUTH_TOKEN": "t"}).ok
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-in-dotenv\n")
    c = check_api_key({})
    assert not c.ok and "export it" in c.detail


def test_models_ok_with_fake_llm() -> None:
    models = runbook_models()
    llm = dry_run_llm(models, models[-1])
    checks, cost = check_models(llm, models, models[-1])
    assert [c.name for c in checks] == [
        "API key valid",
        "model claude-haiku-5-5",
        "model claude-sonnet-5-5",
        "judge structured output (claude-sonnet-5-5)",
    ]
    assert all(c.ok for c in checks) and cost.calls == 3
    ping = llm.calls[0]
    assert ping["max_tokens"] == 1 and ping["effort"] == "low"
    assert ping["model"] == "claude-haiku-5-5"
    assert llm.calls[-1]["output_format"]["type"] == "json_schema"


class StatusError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status_code = status


def test_rejected_key_stops_the_model_checks() -> None:
    llm = FakeLLM(agent=[StatusError(401)])
    checks, _ = check_models(llm, ["m1", "m2"], "m2")
    by = {c.name: c for c in checks}
    assert not by["API key valid"].ok and "HTTP 401" in by["API key valid"].detail
    assert "not tried" in by["model m2"].detail
    assert len(llm.calls) == 1  # nothing else is sent with a bad key


def test_unavailable_model_and_bad_judge_output() -> None:
    ok = LLMResponse([{"type": "text", "text": "OK"}], "max_tokens", Usage(1, 1), "m1")
    bad = LLMResponse([{"type": "text", "text": "nope"}], "end_turn", Usage(1, 1), "m2")
    llm = FakeLLM(agent=[ok, StatusError(404), bad])
    checks, _ = check_models(llm, ["m1", "m2"], "m2")
    by = {c.name: c for c in checks}
    assert by["API key valid"].ok and by["model m1"].ok
    assert not by["model m2"].ok and "not available" in by["model m2"].detail
    assert not by["judge structured output (m2)"].ok


def test_extras() -> None:
    reqs = extra_requirements()
    assert {"api", "embeddings", "frontend", "mcp"} <= set(reqs)
    assert "sentence-transformers" in reqs["embeddings"]
    checks = check_extras({"a": ["x"], "b": ["y", "z"]}, installed=lambda n: n != "z")
    assert [(c.name, c.ok) for c in checks] == [("extra [a]", True), ("extra [b]", False)]
    assert "missing z" in checks[1].detail


def test_metrics_and_writable(tmp_path: Path) -> None:
    assert check_metrics(DEFAULT_BENCHMARK_ROOT / "metrics").ok
    assert not check_metrics(tmp_path).ok  # no metrics here
    blocker = tmp_path / "file"
    blocker.write_text("x")
    ok, bad = check_writable([("a", tmp_path / "new" / "dir"), ("b", blocker / "sub")])
    assert ok.ok and not bad.ok
    assert list((tmp_path / "new" / "dir").iterdir()) == []  # the probe file is removed


def test_benchmark_regeneration_detects_drift(tmp_path: Path) -> None:
    assert check_benchmark_regenerates(DEFAULT_BENCHMARK_ROOT).ok
    root = tmp_path / "bench"
    shutil.copytree(DEFAULT_BENCHMARK_ROOT, root)
    csv = next((root / "data").rglob("*.csv"))
    csv.write_text(csv.read_text() + "tampered\n")
    c = check_benchmark_regenerates(root)
    assert not c.ok and "differs" in c.detail


def test_cost_table_matches_the_plan_estimates() -> None:
    counts = level_counts()
    table, grand = cost_table(counts)
    expected = sum(
        estimate_run(settings_for(a.env), counts).total_usd
        for _exp, _title, arms in PLAN
        for a in arms
    )
    assert grand == pytest.approx(expected)
    assert table.count("\n") == 2 + len(PLAN)  # header, rule, one row per experiment, total
    _, batched = cost_table(counts, batch_judge=True)
    judge = sum(
        estimate_run(settings_for(a.env), counts).judge_usd
        for _exp, _title, arms in PLAN
        for a in arms
    )
    assert batched == pytest.approx(grand - judge / 2)


def _run(**kw: Any) -> tuple[bool, str]:
    lines: list[str] = []
    models = runbook_models()
    ok = run_preflight(
        llm=kw.pop("llm", dry_run_llm(models, models[-1])),
        env=kw.pop("env", {"ANTHROPIC_API_KEY": "dry"}),
        extras=kw.pop("extras", ALL_INSTALLED),
        skip_regenerate=True,
        echo=lines.append,
        **kw,
    )
    return ok, "\n".join(lines)


def test_run_preflight_passes_with_fake_llm(tmp_path: Path) -> None:
    ok, out = _run(experiments_dir=tmp_path / "exp")
    assert ok, out
    assert "pre-flight passed" in out and "| 1 | baseline |" in out and "| 5 | structured |" in out
    assert "[WARN] Message Batches API" in out  # the FakeLLM cannot batch


def test_run_preflight_fails_on_any_failed_check(tmp_path: Path) -> None:
    ok, out = _run(env={}, experiments_dir=tmp_path / "exp")
    assert not ok and "PRE-FLIGHT FAILED" in out and "API key set" in out
    ok, out = _run(
        extras={"embeddings": ["definitely-not-installed-pkg"]},
        require_embeddings=True,
        experiments_dir=tmp_path,
    )
    assert not ok and "extra [embeddings]" in out.split("PRE-FLIGHT FAILED")[1]
    ok, out = _run(extras={"api": ["definitely-not-installed-pkg"]}, experiments_dir=tmp_path)
    assert not ok and "extra [api]" in out.split("PRE-FLIGHT FAILED")[1]


def test_missing_embeddings_extra_warns_unless_asked_for(tmp_path: Path) -> None:
    """Fresh-clone audit: torch is opt-in, so its absence must not fail a plain pre-flight."""
    missing = {"embeddings": ["definitely-not-installed-pkg"]}
    ok, out = _run(extras=missing, require_embeddings=False, experiments_dir=tmp_path)
    assert ok, out
    line = next(ln for ln in out.splitlines() if "extra [embeddings]" in ln)
    assert "[WARN]" in line and "--embeddings" in line and "pre-flight passed" in out
    checks = check_extras(missing, require_embeddings=False)
    assert checks[0].ok and checks[0].warning


def test_embeddings_requested() -> None:
    bm25 = Settings(_env_file=None)
    assert embeddings_requested(True, env={}, settings=bm25)
    assert not embeddings_requested(False, env={}, settings=bm25)
    assert not embeddings_requested(env={"AGENT_SEMANTIC_BACKEND": "bm25"}, settings=bm25)
    for backend in ("embedding", "Hybrid"):
        assert embeddings_requested(env={"AGENT_SEMANTIC_BACKEND": backend}, settings=bm25)
    assert embeddings_requested(
        env={}, settings=Settings(_env_file=None, semantic_backend="hybrid")
    )


def test_dry_run_labels_the_api_checks(tmp_path: Path) -> None:
    ok, out = _run(dry_run=True, experiments_dir=tmp_path)
    assert ok, out
    key = next(ln for ln in out.splitlines() if "API key set" in ln)
    assert "not checked: --dry-run uses a placeholder key" in key
    valid = next(ln for ln in out.splitlines() if "API key valid" in ln)
    assert "dry run (scripted FakeLLM)" in valid


def test_smoke_estimate_uses_limit(tmp_path: Path) -> None:
    _, out = _run(limit=8, experiments_dir=tmp_path)
    assert "(8 tasks:" in out


def test_cli_dry_run_exit_codes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = CliRunner()
    monkeypatch.setattr(preflight, "extra_requirements", lambda: ALL_INSTALLED)
    args = ["--dry-run", "--skip-regenerate", "--experiments-dir", str(tmp_path)]
    res = runner.invoke(preflight.app, args)
    assert res.exit_code == 0, res.output
    monkeypatch.setattr(preflight, "extra_requirements", lambda: {"x": ["no-such-dist-xyz"]})
    res = runner.invoke(preflight.app, args)
    assert res.exit_code == 1 and "PRE-FLIGHT FAILED" in res.output
    monkeypatch.delenv("AGENT_SEMANTIC_BACKEND", raising=False)
    no_torch = {"embeddings": ["no-such-dist-xyz"]}
    monkeypatch.setattr(preflight, "extra_requirements", lambda: no_torch)
    res = runner.invoke(preflight.app, args)
    assert res.exit_code == 0 and "[WARN] extra [embeddings]" in res.output, res.output
    res = runner.invoke(preflight.app, [*args, "--embeddings"])
    assert res.exit_code == 1 and "PRE-FLIGHT FAILED: 1 check(s): extra [embeddings]" in res.output


def test_script_wraps_the_module(tmp_path: Path) -> None:
    script = REPO / "scripts" / "preflight.sh"
    env = {**os.environ, "PYTHON": sys.executable}
    out = subprocess.run(["bash", str(script), "--help"], capture_output=True, text=True, env=env)
    assert out.returncode == 0 and "--dry-run" in out.stdout
    out = subprocess.run(
        ["bash", str(script), "--dry-run", "--skip-regenerate", "--experiments-dir", str(tmp_path)],
        capture_output=True,
        text=True,
        env=env,
    )
    # exit 0 or 1 depending on whether the optional extras are installed here
    assert out.returncode in (0, 1) and "Estimated cost per experiment" in out.stdout
    assert ("pre-flight passed" in out.stdout) == (out.returncode == 0)


def test_check_mark() -> None:
    assert Check("a", True).mark == "ok"
    assert Check("a", True, warning=True).mark == "WARN"
    assert Check("a", False).mark == "FAIL"
