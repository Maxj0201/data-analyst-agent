"""Aggregate eval runs into Markdown reports and compare two runs.

Metric definitions:
- accuracy: passed / scored, where errors and timeouts count as failures. Tasks the run could
  not grade (L3 rubric tasks without an LLM judge, ``score_details.skipped``) are excluded from
  the denominator and reported as "skipped".
- adversarial pass rate: accuracy over ADV tasks.
- latency p50/p95: linear-interpolated percentiles of per-task wall-clock seconds.
- critic intervention rate: share of tasks where the critic asked for at least one revision.
- critic correction rate: among those tasks, the share that ended up passing.
- numeric text-fallback rate: among single-number answers (L1/L2/ADV ``expected_value``), the
  share scored from report text because the agent returned empty ``key_numbers``.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from analyst_agent.eval.runner import load_results
from analyst_agent.eval.schema import DIFFICULTIES
from analyst_agent.eval.scorers import TEXT_FALLBACK_PATH, numeric_path


def percentile(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    xs = sorted(values)
    pos = (len(xs) - 1) * q / 100
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def _rate(num: int, den: int) -> float | None:
    return num / den if den else None


def is_skipped(r: dict[str, Any]) -> bool:
    """The task was not graded (e.g. needs the LLM judge and the run had none)."""
    return bool((r.get("score_details") or {}).get("skipped"))


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(results)
    scored = [r for r in results if not is_skipped(r)]
    by_diff: dict[str, dict[str, Any]] = {}
    for d in DIFFICULTIES:
        rs = [r for r in results if r["difficulty"] == d]
        if not rs:
            continue
        rs_scored = [r for r in rs if not is_skipped(r)]
        by_diff[d] = {
            "n": len(rs),
            "passed": sum(r["passed"] for r in rs),
            "skipped": len(rs) - len(rs_scored),
            "accuracy": _rate(sum(r["passed"] for r in rs_scored), len(rs_scored)),
            "mean_score": (
                sum(r["score"] for r in rs_scored) / len(rs_scored) if rs_scored else None
            ),
            "errors": sum(r["status"] == "error" for r in rs),
            "timeouts": sum(r["status"] == "timeout" for r in rs),
        }
    lat = [r["latency_s"] for r in results]
    costs = [r["cost_usd"] for r in results]
    intervened = [r for r in results if r.get("critic_revisions", 0) > 0]
    paths = [numeric_path(r.get("score_details") or {}) for r in results]
    numeric = [p for p in paths if p is not None]
    adv = by_diff.get("ADV")
    retrievals = [semantic_retrieval(r) for r in results]
    sem_on = [m for m in retrievals if m is not None]
    return {
        "n": n,
        "passed": sum(r["passed"] for r in results),
        "skipped": n - len(scored),
        "accuracy": _rate(sum(r["passed"] for r in scored), len(scored)),
        "by_difficulty": by_diff,
        "adversarial_pass_rate": adv["accuracy"] if adv else None,
        "errors": sum(r["status"] == "error" for r in results),
        "timeouts": sum(r["status"] == "timeout" for r in results),
        "total_cost_usd": sum(costs),
        "mean_cost_usd": sum(costs) / n if n else None,
        "latency_p50_s": percentile(lat, 50) if lat else None,
        "latency_p95_s": percentile(lat, 95) if lat else None,
        "mean_tool_calls": sum(r["tool_calls"] for r in results) / n if n else None,
        "mean_critic_rounds": sum(r["critic_rounds"] for r in results) / n if n else None,
        "critic_intervention_rate": _rate(len(intervened), n),
        "critic_correction_rate": _rate(sum(r["passed"] for r in intervened), len(intervened)),
        "numeric_tasks": len(numeric),
        "text_fallback_rate": _rate(sum(p == TEXT_FALLBACK_PATH for p in numeric), len(numeric)),
        "semantic_layer_rate": _rate(len(sem_on), n),
        "mean_metrics_retrieved": sum(len(m) for m in sem_on) / len(sem_on) if sem_on else None,
    }


def semantic_retrieval(result: dict[str, Any]) -> list[str] | None:
    """Metric names the semantic layer injected for a task, or None if the layer was off."""
    for e in result.get("trace") or []:
        if e.get("type") == "semantic_retrieval":
            return list(e.get("metrics") or [])
    return None


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{100 * x:.1f}%"


def _num(x: float | None, fmt: str = "{:.2f}") -> str:
    return "–" if x is None or (isinstance(x, float) and math.isnan(x)) else fmt.format(x)


def _usd(x: float | None) -> str:
    return _num(x, "${:.4f}")


HEADLINE = [
    ("Tasks", "n", str),
    ("Skipped (not gradable)", "skipped", str),
    ("Accuracy (scored)", "accuracy", _pct),
    ("Adversarial pass rate", "adversarial_pass_rate", _pct),
    ("Errors / timeouts", None, None),
    ("Mean cost / task", "mean_cost_usd", _usd),
    ("Total cost", "total_cost_usd", _usd),
    ("Latency p50", "latency_p50_s", lambda x: _num(x, "{:.1f}s")),
    ("Latency p95", "latency_p95_s", lambda x: _num(x, "{:.1f}s")),
    ("Mean tool calls", "mean_tool_calls", _num),
    ("Mean critic rounds", "mean_critic_rounds", _num),
    ("Critic intervention rate", "critic_intervention_rate", _pct),
    ("Critic correction rate", "critic_correction_rate", _pct),
    ("Numeric text-fallback rate", "text_fallback_rate", _pct),
    ("Semantic layer on", "semantic_layer_rate", _pct),
    ("Mean metrics retrieved", "mean_metrics_retrieved", _num),
]


def _headline_value(s: dict[str, Any], key: str | None, fmt: Any) -> str:
    if key is None:
        return f"{s['errors']} / {s['timeouts']}"
    return fmt(s[key])


def render_report(
    results: list[dict[str, Any]], meta: dict[str, Any] | None = None, title: str = "Eval run"
) -> str:
    s = summarize(results)
    lines = [f"# {title}", ""]
    if meta:
        cfg = meta.get("config", {})
        lines += [
            f"- started: {meta.get('started_at', '?')}  finished: {meta.get('finished_at', '?')}",
            f"- git: `{meta.get('git_sha')}`  agent: `{cfg.get('agent')}`  "
            f"judge: `{cfg.get('judge_model')}`  concurrency: {cfg.get('concurrency')}  "
            f"timeout: {cfg.get('timeout_s')}s  isolation: {cfg.get('isolation', 'inline')}",
            f"- prompts: {_prompt_summary(meta.get('prompts'))}",
            "",
        ]
    lines += ["## Summary", "", "| Metric | Value |", "|---|---|"]
    lines += [f"| {label} | {_headline_value(s, k, f)} |" for label, k, f in HEADLINE]
    lines += [
        "",
        "## Accuracy by difficulty",
        "",
        "| Difficulty | Tasks | Passed | Skipped | Accuracy | Mean score | Errors | Timeouts |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for d, b in s["by_difficulty"].items():
        lines.append(
            f"| {d} | {b['n']} | {b['passed']} | {b['skipped']} | {_pct(b['accuracy'])} | "
            f"{_num(b['mean_score'])} | {b['errors']} | {b['timeouts']} |"
        )
    lines += [
        "",
        "## Tasks",
        "",
        "| Task | Level | Status | Pass | Score | Latency | Cost | Tools | Critic |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(results, key=lambda r: (DIFFICULTIES.index(r["difficulty"]), r["task_id"])):
        lines.append(
            f"| {r['task_id']} | {r['difficulty']} | {r['status']} | "
            f"{_mark(r)} | {r['score']:.2f} | {r['latency_s']:.1f}s | "
            f"{_usd(r['cost_usd'])} | {r['tool_calls']} | "
            f"{r['critic_rounds']}/{r.get('critic_revisions', 0)} |"
        )
    lines += ["", "_Critic column: rounds / revise verdicts._", ""]
    return "\n".join(lines)


def _mark(r: dict[str, Any]) -> str:
    return "⏭️" if is_skipped(r) else "✅" if r["passed"] else "❌"


def _prompt_summary(prompts: dict[str, dict[str, str]] | None) -> str:
    if not prompts:
        return "unknown (run predates prompt versioning)"
    return ", ".join(f"{name}@{p.get('version')}" for name, p in sorted(prompts.items()))


def render_prompt_diff(
    pa: dict[str, dict[str, str]] | None,
    pb: dict[str, dict[str, str]] | None,
    name_a: str = "A",
    name_b: str = "B",
) -> list[str]:
    """Markdown lines describing prompt-version differences between two runs' metadata."""
    lines = ["", "## Prompt versions", ""]
    if not pa or not pb:
        missing = [n for n, p in ((name_a, pa), (name_b, pb)) if not p]
        lines.append(f"- No prompt metadata for {' and '.join(missing)} (run predates versioning).")
        return lines
    rows = []
    for name in sorted(pa.keys() | pb.keys()):
        a, b = pa.get(name), pb.get(name)
        if a == b:
            continue
        if a is None or b is None:
            note = f"only in {name_a if b is None else name_b}"
        elif a.get("version") == b.get("version"):
            note = "⚠️ text changed without a version bump"
        else:
            note = "changed"
        rows.append(f"| {name} | {_pv(a)} | {_pv(b)} | {note} |")
    if not rows:
        lines.append("- Identical prompts in both runs.")
        return lines
    lines += [f"| Prompt | {name_a} | {name_b} | Note |", "|---|---|---|---|", *rows]
    return lines


def _pv(p: dict[str, str] | None) -> str:
    return "–" if p is None else f"{p.get('version')} (`{p.get('sha256')}`)"


def _delta(a: float | None, b: float | None, pct: bool) -> str:
    if (
        a is None
        or b is None
        or (isinstance(a, float) and math.isnan(a))
        or (isinstance(b, float) and math.isnan(b))
    ):
        return "–"
    d = b - a
    return f"{100 * d:+.1f} pp" if pct else f"{d:+.4g}"


def render_compare(
    a: list[dict[str, Any]],
    b: list[dict[str, Any]],
    name_a: str = "A",
    name_b: str = "B",
    meta_a: dict[str, Any] | None = None,
    meta_b: dict[str, Any] | None = None,
) -> str:
    sa, sb = summarize(a), summarize(b)
    lines = [
        f"# Compare: {name_a} vs {name_b}",
        "",
        f"| Metric | {name_a} | {name_b} | Δ |",
        "|---|---|---|---|",
    ]
    pct_keys = {
        "accuracy",
        "adversarial_pass_rate",
        "critic_intervention_rate",
        "critic_correction_rate",
        "text_fallback_rate",
        "semantic_layer_rate",
    }
    for label, k, f in HEADLINE:
        if k is None:
            lines.append(
                f"| {label} | {_headline_value(sa, k, f)} | {_headline_value(sb, k, f)} | |"
            )
            continue
        lines.append(
            f"| {label} | {f(sa[k])} | {f(sb[k])} | {_delta(sa[k], sb[k], k in pct_keys)} |"
        )
    for d in DIFFICULTIES:
        da, db = sa["by_difficulty"].get(d), sb["by_difficulty"].get(d)
        if da or db:
            acc_a = da["accuracy"] if da else None
            acc_b = db["accuracy"] if db else None
            lines.append(
                f"| Accuracy {d} | {_pct(acc_a)} | {_pct(acc_b)} | {_delta(acc_a, acc_b, True)} |"
            )

    if meta_a is not None or meta_b is not None:
        lines += render_prompt_diff(
            (meta_a or {}).get("prompts"), (meta_b or {}).get("prompts"), name_a, name_b
        )

    ra = {r["task_id"]: r for r in a}
    rb = {r["task_id"]: r for r in b}
    fixed = sorted(t for t in ra.keys() & rb.keys() if not ra[t]["passed"] and rb[t]["passed"])
    broke = sorted(t for t in ra.keys() & rb.keys() if ra[t]["passed"] and not rb[t]["passed"])
    only_a, only_b = sorted(ra.keys() - rb.keys()), sorted(rb.keys() - ra.keys())
    lines += [
        "",
        "## Per-task changes",
        "",
        f"- Newly passing in {name_b} ({len(fixed)}): "
        + (", ".join(f"`{t}`" for t in fixed) or "none"),
        f"- Regressions in {name_b} ({len(broke)}): "
        + (", ".join(f"`{t}`" for t in broke) or "none"),
    ]
    if only_a or only_b:
        lines.append(
            f"- Only in {name_a}: {len(only_a)}; only in {name_b}: {len(only_b)} (not comparable)"
        )
    common = sorted(ra.keys() & rb.keys())
    if common:
        lines += [
            "",
            f"| Task | {name_a} | {name_b} | Score Δ | Latency {name_a} | "
            f"Latency {name_b} | Cost {name_a} | Cost {name_b} |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for t in common:
            x, y = ra[t], rb[t]
            lines.append(
                f"| {t} | {_mark(x)} | {_mark(y)} | "
                f"{y['score'] - x['score']:+.2f} | {x['latency_s']:.1f}s | "
                f"{y['latency_s']:.1f}s | {_usd(x['cost_usd'])} | {_usd(y['cost_usd'])} |"
            )
    lines.append("")
    return "\n".join(lines)


def load_meta(run_dir: Path) -> dict[str, Any] | None:
    meta_path = run_dir / "meta.json"
    return json.loads(meta_path.read_text()) if meta_path.exists() else None


def report_run(run_dir: Path) -> str:
    return render_report(load_results(run_dir), load_meta(run_dir), f"Eval run {run_dir.name}")


def compare_runs(a: Path, b: Path) -> str:
    return render_compare(
        load_results(a), load_results(b), a.name, b.name, load_meta(a) or {}, load_meta(b) or {}
    )
