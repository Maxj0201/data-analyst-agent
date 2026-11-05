"""Judge calibration: how often does the L3 rubric judge agree with a human?

``benchmarks/judge_calibration/samples/*.yaml`` holds hand-written reports for L3 tasks, three
per task (one clearly correct, one subtly wrong, one off-topic), each with a human label per
rubric point and an overall pass/fail. :func:`run_calibration` grades every sample with the
same :func:`~analyst_agent.eval.scorers.score_rubric` the benchmark uses and compares:

- **verdict agreement**: judge pass/fail (mean over ``--judge-samples``) vs the human verdict,
  with Cohen's kappa and a confusion matrix;
- **point agreement**: each rubric point's majority score vs the human point label;
- **variance**: with ``n_samples > 1``, the per-sample spread of the rubric score and how many
  samples got a split pass/fail verdict across repeats.

``OracleJudgeLLM`` is a fake ``core.llm.LLM`` that answers with the human labels (and the
human's evidence quotes): ``eval judge-calibrate --fake`` uses it to exercise the whole path
offline; it agrees 100% by construction. Tests use their own fakes.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from analyst_agent.eval.agent import AgentOutput
from analyst_agent.eval.judge import Judge
from analyst_agent.eval.schema import DEFAULT_BENCHMARK_ROOT, Task, load_tasks
from analyst_agent.eval.scorers import RUBRIC_JUDGE, quote_in_report, score_rubric

DEFAULT_CALIBRATION_ROOT = DEFAULT_BENCHMARK_ROOT / "judge_calibration"
KINDS = ("correct", "subtly_wrong", "off_topic")


class CalibrationError(ValueError):
    pass


@dataclass
class LabelPoint:
    score: int
    evidence: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class CalibrationSample:
    id: str
    task_id: str
    kind: str
    report: str
    key_numbers: dict[str, Any]
    points: list[LabelPoint]
    passed: bool
    note: str = ""

    def output(self) -> AgentOutput:
        return AgentOutput(answer=self.report, key_numbers=dict(self.key_numbers))

    @property
    def point_scores(self) -> list[int]:
        return [p.score for p in self.points]


def _parse_sample(doc: dict[str, Any], path: Path) -> CalibrationSample:
    try:
        labels = doc["labels"]
        points = [
            LabelPoint(
                score=int(p["score"]),
                evidence=[str(q) for q in p.get("evidence") or []],
                reason=str(p.get("reason", "")),
            )
            for p in labels["points"]
        ]
        return CalibrationSample(
            id=str(doc["id"]),
            task_id=str(doc["task_id"]),
            kind=str(doc["kind"]),
            report=str(doc["report"]),
            key_numbers=dict(doc.get("key_numbers") or {}),
            points=points,
            passed=bool(labels["passed"]),
            note=str(doc.get("note", "")),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise CalibrationError(f"{path}: malformed sample ({type(e).__name__}: {e})") from e


def validate_sample(s: CalibrationSample, task: Task) -> list[str]:
    """Problems with a sample's labels (empty when consistent)."""
    problems = []
    if s.kind not in KINDS:
        problems.append(f"kind {s.kind!r} not in {KINDS}")
    rubric = task.expected.rubric or []
    if len(s.points) != len(rubric):
        problems.append(f"{len(s.points)} point labels for {len(rubric)} rubric points")
    if any(p.score not in (0, 1) for p in s.points):
        problems.append("point scores must be 0 or 1")
    frac = sum(s.point_scores) / len(rubric) if rubric else 0.0
    if (frac >= task.expected.pass_threshold) != s.passed:
        problems.append(
            f"passed={s.passed} disagrees with points ({frac:.2f} vs threshold "
            f"{task.expected.pass_threshold})"
        )
    for i, p in enumerate(s.points):
        if p.score == 1 and not p.evidence:
            problems.append(f"point {i} is labelled 1 without evidence")
        for q in p.evidence:
            if not quote_in_report(q, s.report):
                problems.append(f"point {i} evidence not found in the report: {q[:60]!r}")
        if p.score == 0 and not p.reason:
            problems.append(f"point {i} is labelled 0 without a reason")
    return problems


def load_samples(
    root: Path = DEFAULT_CALIBRATION_ROOT, benchmarks: Path = DEFAULT_BENCHMARK_ROOT
) -> tuple[list[CalibrationSample], dict[str, Task]]:
    """All samples under ``root/samples`` and the L3 tasks they refer to, validated."""
    files = sorted((root / "samples").glob("*.yaml"))
    if not files:
        raise CalibrationError(f"no calibration samples in {root / 'samples'}")
    samples = [_parse_sample(yaml.safe_load(f.read_text()), f) for f in files]
    ids = [s.id for s in samples]
    if len(set(ids)) != len(ids):
        raise CalibrationError("duplicate sample ids")
    tasks = {t.id: t for t in load_tasks(benchmarks, ids=sorted({s.task_id for s in samples}))}
    problems = []
    for s in samples:
        task = tasks.get(s.task_id)
        if task is None or task.difficulty != "L3":
            problems.append(f"{s.id}: {s.task_id} is not an L3 task")
            continue
        problems += [f"{s.id}: {p}" for p in validate_sample(s, task)]
    if problems:
        raise CalibrationError("invalid calibration samples:\n  " + "\n  ".join(problems))
    return samples, tasks


# --------------------------------------------------------------------------------------------
# Fake judge LLM (offline path)
# --------------------------------------------------------------------------------------------


@dataclass
class _FakeResponse:
    text: str
    stop_reason: str = "end_turn"


class OracleJudgeLLM:
    """A ``core.llm.LLM`` stand-in that returns each sample's human labels as the verdict.

    The sample is recognised by its report text inside the judge prompt's ``<report>`` block.
    """

    supports_batches = False

    def __init__(self, samples: list[CalibrationSample]):
        self._by_report = {s.report.strip(): s for s in samples}
        self.calls = 0

    def create(self, **kw: Any) -> _FakeResponse:
        self.calls += 1
        user = kw["messages"][0]["content"]
        m = re.search(r"<report>\n(.*)\n</report>", user, re.S)
        s = self._by_report.get(m.group(1).strip()) if m else None
        if s is None:
            return _FakeResponse(json.dumps({"points": []}))
        points = [
            {
                "index": i,
                "evidence": p.evidence,
                "unmatched_numbers": [],
                "score": p.score,
                "justification": p.reason or "matches the human label",
            }
            for i, p in enumerate(s.points)
        ]
        return _FakeResponse(json.dumps({"points": points}))


# --------------------------------------------------------------------------------------------
# Agreement
# --------------------------------------------------------------------------------------------


def cohen_kappa(pairs: list[tuple[bool, bool]]) -> float | None:
    """Cohen's kappa for two raters' binary labels; None when undefined (no pairs)."""
    n = len(pairs)
    if not n:
        return None
    po = sum(a == b for a, b in pairs) / n
    pa = sum(a for a, _ in pairs) / n
    pb = sum(b for _, b in pairs) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    if pe == 1:
        return 1.0 if po == 1 else 0.0
    return round((po - pe) / (1 - pe), 4)


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


async def run_calibration(
    samples: list[CalibrationSample],
    tasks: dict[str, Task],
    judge: Judge,
    n_samples: int = 1,
    concurrency: int = 4,
) -> dict[str, Any]:
    """Grade every sample with the rubric judge and compare with the human labels."""
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(s: CalibrationSample) -> dict[str, Any]:
        async with sem:
            sc = await score_rubric(tasks[s.task_id], s.output(), judge, samples=n_samples)
        row: dict[str, Any] = {
            "id": s.id,
            "task_id": s.task_id,
            "kind": s.kind,
            "human_passed": s.passed,
            "human_points": s.point_scores,
            "judge_score": sc.score,
        }
        if sc.details.get("skipped") or sc.details.get("error"):
            row["error"] = sc.details.get("error", "skipped")
            return row
        jp = [int(p["score"]) for p in sc.details["points"]]
        row.update(
            judge_passed=sc.passed,
            judge_points=jp,
            agree=sc.passed == s.passed,
            point_disagreements=[
                {
                    "index": i,
                    "human": h,
                    "judge": j,
                    "justification": sc.details["points"][i].get("justification", ""),
                    "override": sc.details["points"][i].get("override"),
                }
                for i, (h, j) in enumerate(zip(s.point_scores, jp, strict=True))
                if h != j
            ],
        )
        js = sc.details.get("judge_samples")
        if js:
            row["variance"] = js
            # each repeat's verdict vs the human: agreement of a single judge call
            thr = tasks[s.task_id].expected.pass_threshold
            row["calls_agreeing"] = sum((f >= thr) == s.passed for f in js["scores"])
        return row

    rows = await asyncio.gather(*(one(s) for s in samples))
    return summarize_calibration(rows, n_samples)


def summarize_calibration(rows: list[dict[str, Any]], n_samples: int = 1) -> dict[str, Any]:
    graded = [r for r in rows if "error" not in r]
    pairs = [(r["human_passed"], r["judge_passed"]) for r in graded]
    point_pairs = [
        (h, j) for r in graded for h, j in zip(r["human_points"], r["judge_points"], strict=True)
    ]
    confusion = {
        "human_pass_judge_pass": sum(h and j for h, j in pairs),
        "human_pass_judge_fail": sum(h and not j for h, j in pairs),
        "human_fail_judge_pass": sum((not h) and j for h, j in pairs),
        "human_fail_judge_fail": sum((not h) and (not j) for h, j in pairs),
    }
    by_kind = {}
    for kind in KINDS:
        rs = [r for r in graded if r["kind"] == kind]
        pp = [h == j for r in rs for h, j in zip(r["human_points"], r["judge_points"], strict=True)]
        by_kind[kind] = {
            "n": len(rs),
            "verdict_agreement": _rate(sum(r["agree"] for r in rs), len(rs)),
            "point_agreement": _rate(sum(pp), len(pp)),
            "judge_pass_rate": _rate(sum(r["judge_passed"] for r in rs), len(rs)),
        }
    summary: dict[str, Any] = {
        "judge_prompt": {"name": RUBRIC_JUDGE.name, "version": RUBRIC_JUDGE.version},
        "n_samples": len(rows),
        "judge_samples": n_samples,
        "graded": len(graded),
        "errors": len(rows) - len(graded),
        "verdict_agreement": _rate(sum(r["agree"] for r in graded), len(graded)),
        "kappa": cohen_kappa(pairs),
        "point_agreement": _rate(sum(h == j for h, j in point_pairs), len(point_pairs)),
        "point_kappa": cohen_kappa([(bool(h), bool(j)) for h, j in point_pairs]),
        "confusion": confusion,
        "by_kind": by_kind,
    }
    var = [r["variance"] for r in graded if r.get("variance")]
    if var:
        summary["variance"] = {
            "mean_std": round(sum(v["std"] for v in var) / len(var), 4),
            "max_std": round(max(v["std"] for v in var), 4),
            "split_verdicts": sum(not v["unanimous"] for v in var),
            "single_call_agreement": _rate(
                sum(r.get("calls_agreeing", 0) for r in graded),
                sum(len(r["variance"]["scores"]) for r in graded if r.get("variance")),
            ),
        }
    summary["rows"] = rows
    return summary


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{100 * x:.1f}%"


def _kappa(x: float | None) -> str:
    return "–" if x is None else f"{x:.3f}"


def render_calibration(summary: dict[str, Any]) -> str:
    """Markdown report of a calibration run."""
    jp = summary["judge_prompt"]
    lines = [
        "# Judge calibration",
        "",
        f"- judge prompt: `{jp['name']}@{jp['version']}`  samples: {summary['n_samples']}  "
        f"judge calls per sample: {summary['judge_samples']}  errors: {summary['errors']}",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Verdict agreement (pass/fail) | {_pct(summary['verdict_agreement'])} |",
        f"| Cohen's kappa (verdict) | {_kappa(summary['kappa'])} |",
        f"| Rubric-point agreement | {_pct(summary['point_agreement'])} |",
        f"| Cohen's kappa (points) | {_kappa(summary['point_kappa'])} |",
    ]
    v = summary.get("variance")
    if v:
        lines += [
            f"| Single-call verdict agreement | {_pct(v['single_call_agreement'])} |",
            f"| Mean std of rubric score | {v['mean_std']:.3f} |",
            f"| Max std of rubric score | {v['max_std']:.3f} |",
            f"| Samples with a split verdict | {v['split_verdicts']} |",
        ]
    c = summary["confusion"]
    lines += [
        "",
        "## Confusion (verdict)",
        "",
        "| | judge pass | judge fail |",
        "|---|---|---|",
        f"| human pass | {c['human_pass_judge_pass']} | {c['human_pass_judge_fail']} |",
        f"| human fail | {c['human_fail_judge_pass']} | {c['human_fail_judge_fail']} |",
        "",
        "## By sample kind",
        "",
        "| Kind | Samples | Verdict agreement | Point agreement | Judge pass rate |",
        "|---|---|---|---|---|",
    ]
    for kind, b in summary["by_kind"].items():
        lines.append(
            f"| {kind} | {b['n']} | {_pct(b['verdict_agreement'])} | "
            f"{_pct(b['point_agreement'])} | {_pct(b['judge_pass_rate'])} |"
        )
    lines += ["", "## Samples", ""]
    header = "| Sample | Human | Judge | Score | Points (human / judge) |"
    sep = "|---|---|---|---|---|"
    if v:
        header = header[:-1] + " Std | Pass rate |"
        sep += "---|---|"
    lines += [header, sep]
    for r in summary["rows"]:
        human = "pass" if r["human_passed"] else "fail"
        if "error" in r:
            lines.append(f"| {r['id']} | {human} | error | – | {r['error'][:60]} |")
            continue
        judge = ("pass" if r["judge_passed"] else "fail") + ("" if r["agree"] else " ✗")
        pts = "".join(map(str, r["human_points"])) + " / " + "".join(map(str, r["judge_points"]))
        line = f"| {r['id']} | {human} | {judge} | {r['judge_score']:.2f} | `{pts}` |"
        if v:
            rv = r.get("variance") or {}
            line += f" {rv.get('std', 0):.3f} | {_pct(rv.get('pass_rate'))} |"
        lines.append(line)
    dis = [r for r in summary["rows"] if r.get("point_disagreements")]
    if dis:
        lines += ["", "## Point disagreements", ""]
        for r in dis:
            for d in r["point_disagreements"]:
                why = d.get("override") or d.get("justification") or ""
                lines.append(
                    f"- `{r['id']}` point {d['index']}: human {d['human']}, judge {d['judge']}"
                    f" ({why[:160]})"
                )
    lines.append("")
    return "\n".join(lines)
