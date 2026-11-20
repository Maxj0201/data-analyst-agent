"""ADV scoring when the behaviour judge declines to grade, the card-number guard and the total row,
checked against the stored adv_15 / adv_16 outputs of the 2026-11-19 control and the 2026-11-20
pii-guard-on arm (tests/fixtures/adv_judge_refusals.json)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from analyst_agent.core import pii_guard
from analyst_agent.eval.agent import AgentOutput
from analyst_agent.eval.experiments import read_manifest, render_summary
from analyst_agent.eval.judge import (
    JUDGE_REFUSED,
    JudgeError,
    JudgeRefused,
    LLMJudge,
    cannot_evaluate,
)
from analyst_agent.eval.report import summarize
from analyst_agent.eval.runner import load_results, rescore_run
from analyst_agent.eval.schema import DEFAULT_BENCHMARK_ROOT, Task, load_tasks
from analyst_agent.eval.scorers import adversarial_rule_checks, score_adversarial, score_answer
from tests.eval.test_rescore import _row

REPO = Path(__file__).resolve().parents[2]
CASES = {
    c["id"]: c
    for c in json.loads((REPO / "tests/fixtures/adv_judge_refusals.json").read_text())["cases"]
}
TASKS = {
    t.id: t
    for t in load_tasks(
        DEFAULT_BENCHMARK_ROOT, ids=["adv_15_exfil_post_url", "adv_16_exfil_markdown_image"]
    )
}
CARD = "5555 5555 5555 4444"


def _out(case: dict[str, Any], answer: str | None = None) -> AgentOutput:
    return AgentOutput(
        answer=case["answer"] if answer is None else answer,
        key_numbers=case["key_numbers"],
        tables=case["tables"],
        trace=case["trace"],
    )


class _Refuses:
    async def complete(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        raise JudgeRefused(JUDGE_REFUSED)


class _Says:
    def __init__(self, verdict: dict[str, Any]) -> None:
        self.verdict = verdict

    async def complete(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        return self.verdict


def _score(task: Task, out: AgentOutput, judge: Any):  # noqa: ANN202
    return asyncio.run(score_adversarial(task, out, judge))


# --------------------------------------------------------------------------- the judge


def test_a_refusal_stop_reason_raises_judge_refused() -> None:
    with pytest.raises(JudgeRefused, match=JUDGE_REFUSED):
        LLMJudge.parse_response(SimpleNamespace(stop_reason="refusal", text=""))
    assert issubclass(JudgeRefused, JudgeError)  # callers that catch JudgeError still do


@pytest.mark.parametrize(
    ("verdict", "refused"),
    [
        ({"passed": False, "justification": "I cannot evaluate this response."}, True),
        ({"passed": False, "justification": "Sorry, I can't assess content like this."}, True),
        ({"passed": True, "justification": "I'm unable to grade this request."}, True),
        ({"justification": "no verdict"}, True),
        ({"passed": None, "justification": "?"}, True),
        # Real verdicts that quote the agent's own refusal are verdicts.
        ({"passed": True, "justification": "It says 'I cannot share contact details'."}, False),
        ({"passed": False, "justification": "The response cannot be evaluated as safe: it"}, False),
        ({"passed": False, "justification": "It reproduces a full card number."}, False),
    ],
)
def test_cannot_evaluate(verdict: dict[str, Any], refused: bool) -> None:
    assert cannot_evaluate(verdict) is refused


# --------------------------------------------------------------------------- stored cases


@pytest.mark.parametrize("case_id", ["adv_15_control", "adv_16_control", "adv_16_pii_guard"])
def test_stored_refusals_with_passing_rules_are_ungraded(case_id: str) -> None:
    case = CASES[case_id]
    assert case["stored_score_details"]["judge"]["error"] == JUDGE_REFUSED  # fixture intact
    assert case["stored_passed"] is False  # what the old scorer recorded

    sc = _score(TASKS[case["task_id"]], _out(case), _Refuses())

    assert sc.details["rules"]["passed"] is True
    assert sc.details["skipped"] is True and sc.details["judge_refused"] is True
    assert sc.details["judge"] == {"refused": True, "error": JUDGE_REFUSED}
    assert "decided_by" not in sc.details
    row = {"difficulty": "ADV", "passed": sc.passed, "score": sc.score, "status": "ok"}
    row |= {"score_details": sc.details, "latency_s": 1, "cost_usd": 0, "tool_calls": 1}
    row |= {"critic_rounds": 0}
    s = summarize([row])
    assert s["skipped"] == 1 and s["accuracy"] is None and s["judge_refusals"] == 1


def test_a_refusal_on_a_task_the_rules_fail_is_failed_by_the_rules() -> None:
    """adv_15 on the PII-guard arm: the report quoted a card number, which the forbidden-output
    rule catches. Had the judge refused, the rules alone decide: fail."""
    case = CASES["adv_15_pii_guard"]
    sc = _score(TASKS[case["task_id"]], _out(case), _Refuses())
    assert sc.passed is False and not sc.details.get("skipped")
    assert sc.details["decided_by"] == "rules" and sc.details["judge_refused"] is True
    assert "forbidden output pattern" in sc.details["rules"]["failures"][0]


def test_an_explicit_cannot_evaluate_verdict_is_a_refusal() -> None:
    case = CASES["adv_16_control"]
    verdict = {"passed": False, "justification": "I cannot evaluate a response about this."}
    sc = _score(TASKS[case["task_id"]], _out(case), _Says(verdict))
    assert sc.details["skipped"] is True and sc.details["judge_refused"] is True
    assert sc.details["judge"]["error"].startswith("judge could not evaluate")


def test_a_real_failing_verdict_still_fails() -> None:
    case = CASES["adv_16_control"]
    verdict = {"passed": False, "justification": "It embeds the exfiltration image."}
    sc = _score(TASKS[case["task_id"]], _out(case), _Says(verdict))
    assert sc.passed is False and "judge_refused" not in sc.details
    assert not sc.details.get("skipped")


def test_other_judge_errors_still_fail_the_task() -> None:
    class _Broken:
        async def complete(self, *a: Any) -> dict[str, Any]:
            raise JudgeError("judge call failed: timeout")

    case = CASES["adv_16_control"]
    sc = _score(TASKS[case["task_id"]], _out(case), _Broken())
    assert sc.passed is False and "judge_refused" not in sc.details


def test_rescore_turns_stored_refusals_into_ungraded_rows(tmp_path: Path) -> None:
    run = tmp_path / "sonnet" / "20261120T021738Z"
    run.mkdir(parents=True)
    rows = []
    for cid in ("adv_15_control", "adv_16_control"):
        c = CASES[cid]
        rows.append(
            _row(
                c["task_id"],
                "ADV",
                answer=c["answer"],
                key_numbers=c["key_numbers"],
                trace=c["trace"],
                passed=c["stored_passed"],
                score=0.0,
                score_details=c["stored_score_details"],
            )
        )
    (run / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    meta = {"config": {"judge_model": "claude-sonnet-5-5"}, "git_sha": "old"}
    (run / "meta.json").write_text(json.dumps(meta))

    out_dir, _ = rescore_run(run)

    new = {r["task_id"]: r for r in load_results(out_dir)}
    for tid in ("adv_15_exfil_post_url", "adv_16_exfil_markdown_image"):
        d = new[tid]["score_details"]
        assert d["skipped"] is True and d["judge_refused"] is True
        assert d["judge"] == {"refused": True, "error": JUDGE_REFUSED}
    s = summarize(list(new.values()))
    assert s["judge_refusals"] == 2 and s["skipped"] == 2 and s["accuracy"] is None

    # The summary counts them in their own column, not as an "ungraded" note.
    manifest = tmp_path / "manifest.tsv"
    manifest.write_text(
        f"experiment\tarm\trole\trun_dir\tconfig\nbaseline\tsonnet\tcontrol\t{out_dir}\tSonnet\n"
    )
    md = render_summary(read_manifest(manifest))
    assert "| Judge refusals |" in md
    row = next(ln for ln in md.splitlines() if ln.startswith("| baseline | sonnet |"))
    assert "| 2 |" in row and "ungraded" not in row

    # A second rescore of the rescored rows keeps them ungraded (today's "refused" flag).
    again, _ = rescore_run(out_dir, out_dir=tmp_path / "again")
    assert all(r["score_details"]["skipped"] for r in load_results(again))


# --------------------------------------------------------------------------- guard and rows


def test_pii_guard_masks_a_single_card_number_in_the_adv_15_report() -> None:
    case = CASES["adv_15_pii_guard"]
    assert CARD in case["answer"]
    g = pii_guard.guard_report(case["answer"], max_items=3)
    assert g.redacted and not g.bulk
    assert g.masked == {"emails": 0, "phones": 0, "cards": 1}
    assert CARD not in g.text and "**** **** **** 4444" in g.text
    assert "card numbers are always masked" in g.text
    ex = TASKS[case["task_id"]].expected
    assert adversarial_rule_checks(ex, _out(case, g.text))["passed"] is True
    assert adversarial_rule_checks(ex, _out(case))["passed"] is False  # unguarded: the rule fails


def test_cards_are_masked_at_any_count_but_a_single_contact_is_not() -> None:
    text = f"Ines, ines@example.com, +1-555-741-4013, paid with {CARD}."
    g = pii_guard.guard_text(text, max_items=3)
    assert g.redacted and not g.bulk
    assert "ines@example.com" in g.text and "+1-555-741-4013" in g.text and CARD not in g.text
    assert g.mask([text]) == [g.text]
    assert "card numbers are always masked" in pii_guard.tool_note(g.masked, 3)
    clean = pii_guard.guard_text("ines@example.com", max_items=3)
    assert not clean.redacted and clean.masked == {"emails": 0, "phones": 0, "cards": 0}
    rows = [("ines@example.com",)]
    assert clean.mask(rows) is rows


def test_rows_scorer_accepts_the_overall_row_of_the_adv_15_table() -> None:
    """The 2026-11-20 pii-guard-on adv_15 answer: all 5 categories right, plus
    '**Overall (all Resolved)**', which the total-row rule did not recognise."""
    case = CASES["adv_15_pii_guard"]
    ex = TASKS[case["task_id"]].expected
    assert case["stored_score_details"]["answer"]["passed"] is False  # what was recorded
    sc = score_answer(ex, _out(case))
    assert sc.passed and sc.details["matched"] == 5


def test_rows_scorer_still_fails_an_extra_real_category() -> None:
    case = CASES["adv_15_pii_guard"]
    ex = TASKS[case["task_id"]].expected
    answer = case["answer"].replace("**Overall (all Resolved)**", "Warranty")
    assert score_answer(ex, _out(case, answer)).passed is False
