"""Judge calibration set and ``eval judge-calibrate`` (the LLM is always faked)."""

from __future__ import annotations

import json
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from analyst_agent.eval.calibration import (
    DEFAULT_CALIBRATION_ROOT,
    KINDS,
    CalibrationError,
    OracleJudgeLLM,
    cohen_kappa,
    load_samples,
    render_calibration,
    run_calibration,
)
from analyst_agent.eval.cli import app
from analyst_agent.eval.judge import LLMJudge
from analyst_agent.eval.scorers import (
    _NUM_RE,
    extract_numbers,
    numbers_match,
    quote_in_report,
)

SAMPLES, TASKS = load_samples()


# ---------------------------------------------------------------- the calibration set


def test_calibration_set_covers_eight_l3_tasks_with_three_kinds_each() -> None:
    assert len(SAMPLES) == 24
    by_task = Counter(s.task_id for s in SAMPLES)
    assert len(by_task) == 8 and set(by_task.values()) == {3}
    for tid in by_task:
        assert sorted(s.kind for s in SAMPLES if s.task_id == tid) == sorted(KINDS)
        assert TASKS[tid].difficulty == "L3"


def test_human_labels_match_the_sample_kind() -> None:
    for s in SAMPLES:
        if s.kind == "correct":
            assert s.passed, s.id
        else:
            assert not s.passed, s.id
        if s.kind == "subtly_wrong":  # plausible: several points still hold
            assert sum(s.point_scores) >= 2, s.id
        if s.kind == "off_topic":
            assert sum(s.point_scores) <= 1, s.id


DEFINITIONAL = {1.0, 2.0, 3.0, 5.0, 10.0, 30.0, 450.0, 500.0}  # "ratings 1-2", "$500 limit"


def test_every_cited_figure_of_a_correct_report_is_in_key_numbers() -> None:
    # The new judge rule: a result figure must appear in key_numbers. A correct sample must
    # survive it, so check the human evidence of every correct sample mechanically. Years and
    # the numbers of definitions or bucket labels are exempt, as the judge prompt says.
    for s in SAMPLES:
        if s.kind != "correct":
            continue
        kn = [abs(float(v)) for v in s.key_numbers.values()]
        for p in s.points:
            for quote in p.evidence:
                for m in _NUM_RE.finditer(quote):
                    vals = [abs(v) for v in extract_numbers(m.group(0))]
                    if not vals or any(
                        (1990 <= v <= 2100 and v.is_integer()) or v in DEFINITIONAL for v in vals
                    ):
                        continue
                    ok = any(numbers_match(v, k, 0.02) for v in vals for k in kn)
                    assert ok, f"{s.id}: {m.group(0)!r} in {quote[:60]!r} is not in key_numbers"


def test_loader_rejects_inconsistent_labels(tmp_path: Path) -> None:
    root = tmp_path / "cal"
    shutil.copytree(DEFAULT_CALIBRATION_ROOT, root)
    f = root / "samples" / "l3_ecom_returns__correct.yaml"
    f.write_text(f.read_text().replace("passed: true", "passed: false"))
    with pytest.raises(CalibrationError, match="disagrees with points"):
        load_samples(root)
    f.write_text(f.read_text().replace("passed: false", "passed: true"))
    f.write_text(f.read_text().replace('evidence: ["291 of 4,000', 'evidence: ["292 of 4,000'))
    with pytest.raises(CalibrationError, match="evidence not found"):
        load_samples(root)


def test_evidence_quotes_occur_in_their_reports() -> None:
    for s in SAMPLES:
        for p in s.points:
            assert all(quote_in_report(q, s.report) for q in p.evidence), s.id


# ---------------------------------------------------------------- agreement


def test_cohen_kappa() -> None:
    assert cohen_kappa([(True, True), (False, False)]) == 1.0
    assert cohen_kappa([(True, False), (False, True)]) == -1.0
    assert cohen_kappa([]) is None
    assert cohen_kappa([(True, True)] * 3) == 1.0  # one class only, full agreement


@pytest.mark.asyncio
async def test_oracle_fake_agrees_completely() -> None:
    llm = OracleJudgeLLM(SAMPLES)
    summary = await run_calibration(SAMPLES, TASKS, LLMJudge(llm=llm), n_samples=1)
    assert llm.calls == 24
    assert summary["verdict_agreement"] == 1.0 and summary["point_agreement"] == 1.0
    assert summary["kappa"] == 1.0 and summary["errors"] == 0
    assert summary["confusion"] == {
        "human_pass_judge_pass": 8,
        "human_pass_judge_fail": 0,
        "human_fail_judge_pass": 0,
        "human_fail_judge_fail": 16,
    }
    assert summary["judge_prompt"] == {"name": "rubric_judge", "version": "2.0.0"}
    assert "variance" not in summary


class LenientLLM:
    """A judge that ignores the rules: scores every point 1 and cites the first line."""

    def __init__(self) -> None:
        self.calls = 0

    def create(self, **kw: Any) -> Any:
        self.calls += 1
        user = kw["messages"][0]["content"]
        n = user.split("<rubric>")[1].split("</rubric>")[0].strip().count("\n") + 1
        report = user.split("<report>\n", 1)[1].split("\n</report>", 1)[0]
        first = next(ln for ln in report.splitlines() if ln.strip())
        points = [
            {
                "index": i,
                "evidence": [first],
                "unmatched_numbers": [],
                "score": 1,
                "justification": "looks fine",
            }
            for i in range(n)
        ]

        class R:
            text = json.dumps({"points": points})
            stop_reason = "end_turn"

        return R()


@pytest.mark.asyncio
async def test_lenient_judge_is_caught_on_wrong_and_off_topic_samples() -> None:
    summary = await run_calibration(SAMPLES, TASKS, LLMJudge(llm=LenientLLM()))
    assert summary["by_kind"]["correct"]["verdict_agreement"] == 1.0
    assert summary["by_kind"]["subtly_wrong"]["verdict_agreement"] == 0.0
    assert summary["by_kind"]["off_topic"]["judge_pass_rate"] == 1.0
    assert summary["verdict_agreement"] == pytest.approx(8 / 24, abs=1e-4)
    assert summary["confusion"]["human_fail_judge_pass"] == 16
    md = render_calibration(summary)
    assert "## Point disagreements" in md and "✗" in md


class FlakyLLM:
    """Wraps the oracle but flips every point on every third call (judge variance)."""

    def __init__(self) -> None:
        self.oracle = OracleJudgeLLM(SAMPLES)
        self.calls = 0

    def create(self, **kw: Any) -> Any:
        self.calls += 1
        resp = self.oracle.create(**kw)
        if self.calls % 3 == 0:
            v = json.loads(resp.text)
            for p in v["points"]:
                p["score"] = 0
            resp.text = json.dumps(v)
        return resp


@pytest.mark.asyncio
async def test_judge_samples_report_variance() -> None:
    llm = FlakyLLM()
    summary = await run_calibration(SAMPLES, TASKS, LLMJudge(llm=llm), n_samples=3)
    assert llm.calls == 72
    v = summary["variance"]
    assert v["mean_std"] > 0 and v["max_std"] > 0
    # every correct sample got one all-zero call out of three: a split verdict
    assert v["split_verdicts"] >= 1
    assert 0 < v["single_call_agreement"] < 1
    rows = {r["id"]: r for r in summary["rows"]}
    js = rows["l3_ecom_returns__correct"]["variance"]
    assert js["n"] == 3 and len(js["scores"]) == 3
    md = render_calibration(summary)
    assert "Mean std of rubric score" in md and "| Pass rate |" in md


# ---------------------------------------------------------------- CLI


def test_cli_judge_calibrate_fake(tmp_path: Path) -> None:
    out, js = tmp_path / "cal.md", tmp_path / "cal.json"
    res = CliRunner().invoke(
        app,
        [
            "judge-calibrate",
            "--fake",
            "--judge-samples",
            "2",
            "--out",
            str(out),
            "--json",
            str(js),
            "--fail-under",
            "0.9",
        ],
    )
    assert res.exit_code == 0, res.output
    assert "# Judge calibration" in res.output and "Verdict agreement" in res.output
    data = json.loads(js.read_text())
    assert data["verdict_agreement"] == 1.0 and data["judge_samples"] == 2
    assert data["judge_model"] == "fake" and data["variance"]["split_verdicts"] == 0
    assert out.read_text().startswith("# Judge calibration")


def test_cli_judge_calibrate_task_filter_and_no_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    res = CliRunner().invoke(app, ["judge-calibrate", "--fake", "--task", "l3_hr_attrition"])
    assert res.exit_code == 0 and "l3_hr_attrition__off_topic" in res.output
    assert "l3_ecom_returns" not in res.output
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    res = CliRunner().invoke(app, ["judge-calibrate"])
    assert res.exit_code == 2 and "--fake" in res.output


def test_cli_run_records_judge_samples(tmp_path: Path) -> None:
    res = CliRunner().invoke(
        app,
        [
            "run",
            "--agent",
            "oracle",
            "--task",
            "l1_ecom_cancelled_orders",
            "--no-judge",
            "--isolation",
            "inline",
            "--judge-samples",
            "3",
            "--runs-dir",
            str(tmp_path),
        ],
    )
    assert res.exit_code == 0, res.output
    (run_dir,) = tmp_path.iterdir()
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["config"]["judge_samples"] == 3
    assert meta["prompts"]["rubric_judge"]["version"] == "2.0.0"
