"""The stricter L3 rubric judge: evidence citations, the key-numbers rule, judge variance and
the judge prompt's version pin. The LLM is always mocked."""

from __future__ import annotations

import json
from typing import Any

import pytest

from analyst_agent.eval.judge import JudgeError, JudgeUnavailable
from analyst_agent.eval.report import render_report
from analyst_agent.eval.scorers import (
    BEHAVIOR_JUDGE,
    RUBRIC_JUDGE,
    RUBRIC_SCHEMA,
    RUBRIC_SYSTEM,
    build_rubric_prompt,
    judge_prompt_versions,
    quote_in_report,
    score_rubric,
    score_task,
)
from tests.eval.conftest import make_task, out

# When you edit a judge prompt: bump its version in eval/scorers.py, then update its line here.
PINNED = {
    "rubric_judge": ("2.0.0", "e5e40d9d4d3a"),
    "rubric_judge_lenient": ("1.0.0", "16244837415e"),
    "behavior_judge": ("1.0.0", "6e9995cfd725"),
}


class ScriptedJudge:
    """Returns the queued verdicts in order (one per call); records the calls."""

    def __init__(self, *verdicts: dict[str, Any] | Exception):
        self.verdicts = list(verdicts)
        self.calls: list[dict[str, Any]] = []

    async def complete(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"system": system, "user": user, "schema": schema})
        v = self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]
        if isinstance(v, Exception):
            raise v
        return v


def pt(i: int, score: int, evidence: list[str] | None = None, unmatched: list[str] | None = None):
    return {
        "index": i,
        "evidence": evidence or [],
        "unmatched_numbers": unmatched or [],
        "score": score,
        "justification": f"point {i}",
    }


REPORT = (
    "## Returns\n\nThe overall return rate is **7.3%** of orders.\n\n"
    "Apparel has the highest return rate (15.5%).\n"
)
KN = {"return_rate": 0.073, "apparel_return_rate": 0.155}
S1 = "The overall return rate is 7.3% of orders."
S2 = "Apparel has the highest return rate (15.5%)."


# ---------------------------------------------------------------- prompt


def test_rubric_prompt_requires_citations_and_key_numbers() -> None:
    assert "verbatim" in RUBRIC_SYSTEM and "<key_numbers>" in RUBRIC_SYSTEM
    assert "unmatched_numbers" in RUBRIC_SYSTEM
    item = RUBRIC_SCHEMA["properties"]["points"]["items"]
    assert {"evidence", "unmatched_numbers", "score"} <= set(item["required"])
    task = make_task("L3", rubric=["a", "b"])
    user = build_rubric_prompt(task, out(REPORT, key_numbers=KN))
    assert "<report>\n" + REPORT + "\n</report>" in user
    assert '"return_rate": 0.073' in user.split("<key_numbers>")[1]
    assert "<key_numbers>\n(none)\n</key_numbers>" in build_rubric_prompt(task, out(REPORT))


def test_judge_prompt_versions_are_pinned() -> None:
    current = {k: (v["version"], v["sha256"]) for k, v in judge_prompt_versions().items()}
    for name, (version, sha) in PINNED.items():
        if current[name][1] != sha:
            assert current[name][0] != version, (
                f"judge prompt {name!r} changed but its version is still {version}: bump it in "
                "eval/scorers.py and update PINNED in this test"
            )
    assert current == PINNED, "update PINNED in tests/eval/test_judge_quality.py"
    # The fingerprint covers the system prompt, the output instructions and the schema.
    assert RUBRIC_SYSTEM in RUBRIC_JUDGE.text
    assert json.dumps(RUBRIC_SCHEMA, sort_keys=True) in RUBRIC_JUDGE.text
    assert BEHAVIOR_JUDGE.name == "behavior_judge"


# ---------------------------------------------------------------- citations


def test_quote_in_report_tolerates_markdown_and_ellipsis() -> None:
    assert quote_in_report(S1, REPORT)  # "**7.3%**" in the report
    assert quote_in_report("the OVERALL return   rate", REPORT)
    assert quote_in_report("The overall return rate ... of orders.", REPORT)
    assert not quote_in_report("The overall return rate is 8.4% of orders.", REPORT)
    assert not quote_in_report("", REPORT)


@pytest.mark.asyncio
async def test_point_needs_evidence_that_occurs_in_the_report() -> None:
    task = make_task("L3", rubric=["rate", "category", "trend"], pass_threshold=0.6)
    judge = ScriptedJudge(
        {
            "points": [
                pt(0, 1, [S1]),
                pt(1, 1, []),  # no citation
                pt(2, 1, ["Returns doubled in Q4."]),  # invented sentence
            ]
        }
    )
    s = await score_rubric(task, out(REPORT, key_numbers=KN), judge)
    pts = s.details["points"]
    assert [p["score"] for p in pts] == [1, 0, 0]
    assert "override" not in pts[0] and pts[0]["evidence"] == [S1]
    assert pts[1]["override"] == "no evidence cited"
    assert pts[2]["override"] == "cited evidence does not occur in the report"
    assert s.score == pytest.approx(1 / 3, abs=1e-4) and not s.passed


@pytest.mark.asyncio
async def test_figure_missing_from_key_numbers_zeroes_the_point() -> None:
    task = make_task("L3", rubric=["rate", "category"], pass_threshold=1.0)
    judge = ScriptedJudge({"points": [pt(0, 1, [S1]), pt(1, 1, [S2], unmatched=["15.5%"])]})
    s = await score_rubric(task, out(REPORT, key_numbers={"return_rate": 0.073}), judge)
    assert [p["score"] for p in s.details["points"]] == [1, 0]
    assert s.details["points"][1]["override"] == "figures not in key_numbers: 15.5%"
    assert s.details["points"][1]["unmatched_numbers"] == ["15.5%"]
    assert not s.passed


@pytest.mark.asyncio
async def test_zero_scores_are_never_raised_and_keep_their_evidence() -> None:
    task = make_task("L3", rubric=["rate"], pass_threshold=0.5)
    s = await score_rubric(task, out(REPORT), ScriptedJudge({"points": [pt(0, 0, [S1])]}))
    assert s.details["points"][0]["score"] == 0 and "override" not in s.details["points"][0]


# ---------------------------------------------------------------- judge variance


def verdict(*scores: int) -> dict[str, Any]:
    return {"points": [pt(i, sc, [S1]) for i, sc in enumerate(scores)]}


@pytest.mark.asyncio
async def test_judge_samples_report_mean_and_spread() -> None:
    task = make_task("L3", rubric=["a", "b", "c", "d"], pass_threshold=0.75)
    judge = ScriptedJudge(verdict(1, 1, 1, 1), verdict(1, 1, 1, 0), verdict(1, 0, 1, 0))
    s = await score_rubric(task, out(REPORT, key_numbers=KN), judge, samples=3)
    assert len(judge.calls) == 3
    js = s.details["judge_samples"]
    assert js["n"] == 3 and js["ok"] == 3 and js["errors"] == []
    assert sorted(js["scores"]) == [0.5, 0.75, 1.0]
    assert js["mean"] == 0.75 and js["min"] == 0.5 and js["max"] == 1.0
    assert js["std"] == pytest.approx(0.25)
    assert js["pass_rate"] == pytest.approx(2 / 3, abs=1e-4) and js["unanimous"] is False
    assert s.score == 0.75 and s.passed  # mean reaches the threshold
    pts = s.details["points"]
    assert [p["score"] for p in pts] == [1, 1, 1, 0]  # majority per point
    assert [p["mean_score"] for p in pts] == [1.0, pytest.approx(0.6667, abs=1e-4), 1.0, 0.3333]


@pytest.mark.asyncio
async def test_one_sample_keeps_the_old_shape() -> None:
    task = make_task("L3", rubric=["a", "b"], pass_threshold=0.5)
    s = await score_rubric(task, out(REPORT), ScriptedJudge(verdict(1, 0)))
    assert "judge_samples" not in s.details and "mean_score" not in s.details["points"][0]


@pytest.mark.asyncio
async def test_failed_samples_are_dropped_and_all_failing_is_an_error() -> None:
    task = make_task("L3", rubric=["a"], pass_threshold=1.0)
    judge = ScriptedJudge(JudgeError("503"), verdict(1))
    s = await score_rubric(task, out(REPORT), judge, samples=2)
    assert s.passed and s.details["judge_samples"]["ok"] == 1
    assert s.details["judge_samples"]["errors"] == ["503"]

    s = await score_rubric(task, out(REPORT), ScriptedJudge(JudgeError("boom")), samples=3)
    assert not s.passed and s.details["error"] == "boom"
    s = await score_rubric(task, out(REPORT), ScriptedJudge(JudgeUnavailable("no key")), samples=2)
    assert s.details["skipped"] is True


@pytest.mark.asyncio
async def test_score_task_passes_judge_samples_to_l3_only() -> None:
    l3 = make_task("L3", rubric=["a"], pass_threshold=1.0)
    judge = ScriptedJudge(verdict(1))
    await score_task(l3, out(REPORT), judge, judge_samples=4)
    assert len(judge.calls) == 4
    adv = make_task("ADV")
    judge = ScriptedJudge({"passed": True, "justification": "ok"})
    await score_task(adv, out("fine"), judge, judge_samples=4)
    assert len(judge.calls) == 1


def test_report_shows_judge_variance_section() -> None:
    row = {
        "task_id": "l3_x",
        "difficulty": "L3",
        "tags": [],
        "status": "ok",
        "passed": True,
        "score": 0.75,
        "score_details": {
            "method": "rubric_judge",
            "judge_samples": {
                "n": 3,
                "ok": 3,
                "scores": [0.5, 0.75, 1.0],
                "mean": 0.75,
                "std": 0.25,
                "min": 0.5,
                "max": 1.0,
                "pass_rate": 0.6667,
                "unanimous": False,
            },
        },
        "latency_s": 1.0,
        "cost_usd": 0.0,
        "tool_calls": 0,
        "critic_rounds": 0,
    }
    md = render_report([row])
    assert "## Judge variance" in md
    assert "1 tasks judged 3 times each" in md and "1 task(s) with a split" in md
    assert "| l3_x | 0.75 | 0.250 | 0.50 | 1.00 | 66.7% | 3/3 |" in md
    assert "## Judge variance" not in render_report([{**row, "score_details": {}}])
