"""Cost estimates and the comparison summary for the five benchmark experiments.

``python -m analyst_agent.eval.experiments``:

* ``estimate`` – USD estimate for one eval run over the selected tasks, for the configuration in
  the current ``AGENT_*`` environment (the same environment ``eval run`` would see), priced with
  :data:`analyst_agent.core.llm.PRICING`. ``scripts/run_experiments.sh`` uses it to set each
  arm's ``--budget-usd`` cap.
* ``plan`` – the estimate table for the canonical experiment plan (:data:`PLAN`), as pasted in
  ``docs/eval-runbook.md``.
* ``summary`` – read a manifest of finished runs and write ``runs/summary.md``: one overview
  table and one comparison table per experiment, ready to paste into the README.

The token profiles below are planning assumptions, not measurements: replace them with the
mean tokens per level from a real run (``results.jsonl``) once you have one. See
docs/eval-runbook.md.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import typer

from analyst_agent.core.config import Settings
from analyst_agent.core.llm import PRICING, Usage, estimate_cost_usd
from analyst_agent.eval.report import (
    DIFFICULTIES,
    JUDGE_MODE_LABELS,
    JudgeModeMismatch,
    check_same_judge_mode,
    judge_mode,
    load_meta,
    semantic_retrieval,
    summarize,
    summarize_routing,
)
from analyst_agent.eval.runner import load_results
from analyst_agent.eval.schema import DEFAULT_BENCHMARK_ROOT, load_tasks

# --------------------------------------------------------------------------------------------
# Token profiles (planning assumptions; per task, summed over every call of that role)
# --------------------------------------------------------------------------------------------

# Calibrated on the 8-task smoke run of 2026-11-06 (scripts/run_experiments.sh --limit 8
# --only baseline): 2 tasks per level and model, so treat these as ±50% until the full baseline
# is in. The run recorded agent + critic tokens together (same model); the critic's share of the
# uncached input / output is kept from the earlier assumptions (about 25-32% / 17-29%).
#
# Agent loop, Sonnet 5.5 (the control and the default for any model without its own profile).
# The system prompt + tool schemas are cached and shared across a run's tasks, so later tasks
# mostly read the cache (the L3 / ADV tasks wrote none); uncached input is the conversation.
AGENT_PROFILE: dict[str, Usage] = {
    "L1": Usage(2_500, 700, 3_200, 4_600),  # ~4 tool calls
    "L2": Usage(7_700, 1_500, 2_300, 9_000),  # ~6
    "L3": Usage(21_200, 4_100, 0, 14_700),  # ~11
    "ADV": Usage(3_800, 600, 0, 7_800),  # ~4
}
# Critic: one review per task for Sonnet in the smoke run, uncached.
CRITIC_PROFILE: dict[str, Usage] = {
    "L1": Usage(1_200, 200),
    "L2": Usage(2_800, 400),
    "L3": Usage(7_100, 800),
    "ADV": Usage(1_700, 200),
}
# Models that used markedly different token counts: (agent, critic) per level, keyed by the
# agent's model (the critic reviews that model's drafts). Haiku 5.5 took about twice the tool
# calls on L3 (~22) and 1.5 / 2.0 critic reviews on L2 / L3.
MODEL_PROFILES: dict[str, tuple[dict[str, Usage], dict[str, Usage]]] = {
    "claude-haiku-5-5": (
        {
            "L1": Usage(2_900, 1_200, 3_200, 4_600),
            "L2": Usage(17_700, 4_200, 2_300, 11_700),
            "L3": Usage(118_600, 22_300, 0, 29_400),
            "ADV": Usage(4_300, 1_200, 0, 7_800),
        },
        {
            "L1": Usage(1_300, 500),
            "L2": Usage(6_500, 1_200),
            "L3": Usage(39_500, 4_500),
            "ADV": Usage(1_900, 500),
        },
    ),
}


def agent_profile(model: str, level: str) -> Usage:
    return MODEL_PROFILES[model][0][level] if model in MODEL_PROFILES else AGENT_PROFILE[level]


def critic_profile(agent_model: str, level: str) -> Usage:
    if agent_model in MODEL_PROFILES:
        return MODEL_PROFILES[agent_model][1][level]
    return CRITIC_PROFILE[level]


# LLM judge (effort low): L3 rubric grading, ADV behaviour grading. L1/L2 are not judged. The
# smoke run's judge totals, split between L3 and ADV by prompt and verdict size.
JUDGE_PROFILE: dict[str, Usage] = {
    "L3": Usage(4_000, 740),
    "ADV": Usage(860, 110),
}
# Not exercised by the smoke run (features off): still planning assumptions.
ROUTER_PROFILE = Usage(1_500, 150)  # one classifier call per task
# Semantic layer: top-k definitions in the first user message, re-read from cache each turn.
SEMANTIC_EXTRA = Usage(1_200, 0, 0, 6_000)
# Structured report: the larger final_report schema + consistency notes for the critic.
STRUCTURED_EXTRA = Usage(600, 800)
# Routing tier assumed per labelled difficulty (ADV tasks are mostly single-table lookups).
ASSUMED_TIER = {"L1": "L1", "L2": "L2", "L3": "L3", "ADV": "L2"}


def _scaled(u: Usage, k: float = 1.0) -> Usage:
    return Usage(
        int(u.input_tokens * k),
        int(u.output_tokens * k),
        int(u.cache_creation_input_tokens * k),
        int(u.cache_read_input_tokens * k),
    )


def _plus(a: Usage, b: Usage) -> Usage:
    return Usage(
        a.input_tokens + b.input_tokens,
        a.output_tokens + b.output_tokens,
        a.cache_creation_input_tokens + b.cache_creation_input_tokens,
        a.cache_read_input_tokens + b.cache_read_input_tokens,
    )


@dataclass
class Estimate:
    n_tasks: int
    agent_usd: float = 0.0
    critic_usd: float = 0.0
    router_usd: float = 0.0
    judge_usd: float = 0.0
    by_level: dict[str, float] = field(default_factory=dict)
    models: dict[str, str] = field(default_factory=dict)

    @property
    def total_usd(self) -> float:
        return self.agent_usd + self.critic_usd + self.router_usd + self.judge_usd

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_tasks": self.n_tasks,
            "agent_usd": round(self.agent_usd, 4),
            "critic_usd": round(self.critic_usd, 4),
            "router_usd": round(self.router_usd, 4),
            "judge_usd": round(self.judge_usd, 4),
            "total_usd": round(self.total_usd, 4),
            "by_level": {k: round(v, 4) for k, v in self.by_level.items()},
            "models": self.models,
        }


def estimate_run(
    settings: Settings, counts: dict[str, int], judge: bool = True, batch_judge: bool = False
) -> Estimate:
    """Estimated USD for one ``eval run`` of ``counts`` tasks per level under ``settings``.

    ``batch_judge``: the judge calls go through the Message Batches API (``eval run --batch``)
    and are priced at :data:`~analyst_agent.core.llm.BATCH_DISCOUNT`.
    """
    est = Estimate(n_tasks=sum(counts.values()))
    critic_on = settings.max_critic_rounds > 0
    judge_model = settings.judge_model or settings.model
    for level, n in counts.items():
        if not n:
            continue
        model = (
            settings.model_for_tier(ASSUMED_TIER[level])
            if settings.routing_enabled
            else settings.model
        )
        agent = agent_profile(model, level)
        if settings.semantic_layer_enabled:
            agent = _plus(agent, SEMANTIC_EXTRA)
        if settings.structured_report_enabled:
            agent = _plus(agent, STRUCTURED_EXTRA)
        a = n * estimate_cost_usd(model, agent, single_request=False)
        c = (
            n
            * estimate_cost_usd(
                settings.resolved_critic_model,
                critic_profile(model, level),
                single_request=False,
            )
            if critic_on
            else 0.0
        )
        r = (
            n * estimate_cost_usd(settings.router_model, ROUTER_PROFILE, single_request=False)
            if settings.routing_enabled
            else 0.0
        )
        j = (
            n
            * estimate_cost_usd(
                judge_model, JUDGE_PROFILE[level], batch=batch_judge, single_request=False
            )
            if judge and level in JUDGE_PROFILE
            else 0.0
        )
        est.agent_usd += a
        est.critic_usd += c
        est.router_usd += r
        est.judge_usd += j
        est.by_level[level] = a + c + r + j
    est.models = {
        "agent": settings.model,
        "critic": settings.resolved_critic_model if critic_on else "off",
        "judge": judge_model if judge else "off",
    }
    if settings.routing_enabled:
        est.models["router"] = settings.router_model
        est.models["tiers"] = "/".join(settings.model_for_tier(t) for t in ("L1", "L2", "L3"))
    unpriced = sorted(
        {n for m in est.models.values() for n in m.split("/") if n != "off" and n not in PRICING}
    )
    if unpriced:  # estimate_cost_usd bills these at DEFAULT_PRICE
        est.models["unpriced"] = ", ".join(unpriced)
    return est


def level_counts(
    benchmarks: Path = DEFAULT_BENCHMARK_ROOT,
    difficulty: list[str] | None = None,
    limit: int | None = None,
) -> dict[str, int]:
    tasks = load_tasks(benchmarks, difficulty, None, None, limit)
    return {d: sum(t.difficulty == d for t in tasks) for d in DIFFICULTIES}


# --------------------------------------------------------------------------------------------
# The canonical plan (mirrors scripts/run_experiments.sh)
# --------------------------------------------------------------------------------------------

HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"
JUDGE = SONNET

# Every arm pins every feature flag so a local .env cannot leak into a comparison.
BASE_ENV: dict[str, str] = {
    "AGENT_MODEL": SONNET,
    "AGENT_CRITIC_MODEL": "",
    "AGENT_MAX_CRITIC_ROUNDS": "2",
    "AGENT_ROUTING_ENABLED": "false",
    "AGENT_MODEL_L1": "",
    "AGENT_MODEL_L2": "",
    "AGENT_MODEL_L3": "",
    "AGENT_SEMANTIC_LAYER_ENABLED": "false",
    "AGENT_SEMANTIC_BACKEND": "bm25",
    "AGENT_STRUCTURED_REPORT_ENABLED": "false",
    "AGENT_MEMORY_ENABLED": "false",
    "AGENT_APPROVAL_ENABLED": "false",
    "AGENT_JUDGE_MODEL": JUDGE,
}


@dataclass(frozen=True)
class Arm:
    name: str
    env: dict[str, str]  # overrides of BASE_ENV


# Arm "sonnet" of the baseline is the shared control of experiments 2-5 (run once).
CONTROL = "sonnet"
PLAN: list[tuple[str, str, list[Arm]]] = [
    (
        "baseline",
        "Baseline model: Haiku vs Sonnet (all features off)",
        [Arm("haiku", {"AGENT_MODEL": HAIKU}), Arm(CONTROL, {})],
    ),
    (
        "critic",
        "Critic on vs off (Sonnet)",
        [Arm("critic-off", {"AGENT_MAX_CRITIC_ROUNDS": "0"})],
    ),
    (
        "semantic",
        "Semantic layer: off vs bm25 / embedding / hybrid (Sonnet)",
        [
            Arm(
                f"semantic-{b}",
                {"AGENT_SEMANTIC_LAYER_ENABLED": "true", "AGENT_SEMANTIC_BACKEND": b},
            )
            for b in ("bm25", "embedding", "hybrid")
        ],
    ),
    (
        "routing",
        "Model routing on vs off (L1 Haiku, L2/L3 Sonnet)",
        [
            Arm(
                "routing-on",
                {
                    "AGENT_ROUTING_ENABLED": "true",
                    "AGENT_MODEL_L1": HAIKU,
                    "AGENT_MODEL_L2": SONNET,
                    "AGENT_MODEL_L3": SONNET,
                },
            )
        ],
    ),
    (
        "structured",
        "Structured report on vs off (Sonnet)",
        [Arm("structured-on", {"AGENT_STRUCTURED_REPORT_ENABLED": "true"})],
    ),
]


def settings_for(env: dict[str, str]) -> Settings:
    """Settings for an arm's environment, ignoring the caller's own env and .env file."""
    merged = {**BASE_ENV, **env}
    values: dict[str, Any] = {}
    for key, raw in merged.items():
        name = key.removeprefix("AGENT_").lower()
        values[name] = None if raw == "" else raw
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def _short(model: str) -> str:
    return "/".join(m.removeprefix("claude-") for m in model.split("/"))


def plan_table(counts: dict[str, int], cap_factor: float = 1.5) -> str:
    """Markdown table of per-arm estimates for :data:`PLAN` (the shared control listed once)."""
    lines = [
        "| Experiment | Arm | Agent model | Critic | Judge | Agent $ | Critic $ | Router $ | "
        f"Judge $ | **Est. total** | Cap (×{cap_factor:g}) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    grand = 0.0
    for exp, _title, arms in PLAN:
        for arm in arms:
            est = estimate_run(settings_for(arm.env), counts)
            m = est.models
            agent = _short(m.get("tiers", m["agent"]))
            if "router" in m:
                agent = f"L1/L2/L3 {agent}, router {_short(m['router'])}"
            lines.append(
                f"| {exp} | {arm.name} | {agent} | {_short(m['critic'])} | {_short(m['judge'])} "
                f"| ${est.agent_usd:.2f} | ${est.critic_usd:.2f} | ${est.router_usd:.2f} "
                f"| ${est.judge_usd:.2f} | **${est.total_usd:.2f}** "
                f"| ${est.total_usd * cap_factor:.2f} |"
            )
            grand += est.total_usd
    lines.append(f"| **all** | | | | | | | | | **${grand:.2f}** | **${grand * cap_factor:.2f}** |")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------------------------


@dataclass
class ManifestRow:
    experiment: str
    arm: str
    run_dir: Path
    config: str
    role: str  # control | treatment


def read_manifest(path: Path) -> list[ManifestRow]:
    rows: list[ManifestRow] = []
    with open(path, newline="") as f:
        for rec in csv.DictReader(f, delimiter="\t"):
            rows.append(
                ManifestRow(
                    experiment=rec["experiment"],
                    arm=rec["arm"],
                    run_dir=Path(rec["run_dir"]),
                    config=rec.get("config", ""),
                    role=rec.get("role", "treatment"),
                )
            )
    return rows


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{100 * x:.1f}%"


def _pp(a: float | None, b: float | None) -> str:
    return "–" if a is None or b is None else f"{100 * (b - a):+.1f} pp"


def _usd(x: float | None) -> str:
    return "–" if x is None else f"${x:.4f}" if x < 0.1 else f"${x:.3f}"


def _ratio(a: float | None, b: float | None) -> str:
    if not a or b is None:
        return "–"
    return f"{b / a:.2f}×"


@dataclass
class ArmStats:
    row: ManifestRow
    summary: dict[str, Any]
    meta: dict[str, Any]
    results: list[dict[str, Any]]

    @property
    def judge_usd(self) -> float:
        jc = self.meta.get("judge_cost") or {}
        return float(jc.get("usd", 0.0) or 0.0)

    @property
    def total_usd(self) -> float:
        return self.summary["total_cost_usd"] + self.judge_usd

    def level_acc(self, d: str) -> float | None:
        b = self.summary["by_difficulty"].get(d)
        return None if b is None else b["accuracy"]

    @property
    def notes(self) -> str:
        s, out = self.summary, []
        if s["over_budget"]:
            out.append(f"{s['over_budget']} not started (budget)")
        if s["errors"] or s["timeouts"]:
            out.append(f"{s['errors']} err / {s['timeouts']} timeout")
        judged = (self.meta.get("config") or {}).get("judge_model")
        if not judged and any(r["difficulty"] == "L3" for r in self.results):
            out.append("no judge: L3 skipped")
        elif s["skipped"] > s["over_budget"]:
            out.append(f"{s['skipped'] - s['over_budget']} ungraded")
        errs = sum(
            1
            for r in self.results
            for e in r.get("trace") or []
            if e.get("type") == "semantic_retrieval" and e.get("error")
        )
        if errs:
            out.append(f"{errs} retrieval errors")
        return "; ".join(out)


def load_arm(row: ManifestRow) -> ArmStats:
    results = load_results(row.run_dir)
    return ArmStats(row, summarize(results), load_meta(row.run_dir) or {}, results)


OVERVIEW_HEAD = (
    "| Experiment | Arm | Accuracy | L1 | L2 | L3 | ADV | $/task | Total $ (incl. judge) | "
    "p50 latency | Notes |\n|---|---|---|---|---|---|---|---|---|---|---|"
)


def _overview_row(exp: str, a: ArmStats) -> str:
    s = a.summary
    return (
        f"| {exp} | {a.row.arm} | {_pct(s['accuracy'])} | "
        + " | ".join(_pct(a.level_acc(d)) for d in DIFFICULTIES)
        + f" | {_usd(s['mean_cost_usd'])} | {_usd(a.total_usd)} | "
        f"{s['latency_p50_s']:.1f}s | {a.notes} |"
    )


def _extra_columns(exp: str) -> list[tuple[str, Any]]:
    """Experiment-specific columns: (header, fn(ArmStats) -> str)."""
    if exp == "critic":
        return [
            ("Critic rounds", lambda a: f"{a.summary['mean_critic_rounds']:.2f}"),
            ("Intervention", lambda a: _pct(a.summary["critic_intervention_rate"])),
            ("Correction", lambda a: _pct(a.summary["critic_correction_rate"])),
        ]
    if exp == "semantic":

        def retrieved(a: ArmStats) -> str:
            m = a.summary["mean_metrics_retrieved"]
            return "–" if m is None else f"{m:.1f}"

        def err_rate(a: ArmStats) -> str:
            hits = [semantic_retrieval(r) for r in a.results]
            on = [r for r, h in zip(a.results, hits, strict=True) if h is not None]
            errs = sum(
                any(e.get("type") == "semantic_retrieval" and e.get("error") for e in r["trace"])
                for r in on
            )
            return "–" if not on else _pct(errs / len(on))

        return [
            ("Layer on", lambda a: _pct(a.summary["semantic_layer_rate"])),
            ("Metrics retrieved", retrieved),
            ("Retrieval errors", err_rate),
        ]
    if exp == "routing":

        def esc(a: ArmStats) -> str:
            rt = summarize_routing(a.results)
            return "–" if rt is None else _pct(rt["escalation_rate"])

        def tiers(a: ArmStats) -> str:
            rt = summarize_routing(a.results)
            if rt is None:
                return "–"
            return " / ".join(f"{t}:{g['n']}" for t, g in rt["by_tier"].items()) or "–"

        return [("Routed tiers", tiers), ("Escalation", esc)]
    if exp == "structured":
        return [
            ("Numeric text-fallback", lambda a: _pct(a.summary["text_fallback_rate"])),
            ("Critic rounds", lambda a: f"{a.summary['mean_critic_rounds']:.2f}"),
        ]
    return [("Mean tool calls", lambda a: f"{a.summary['mean_tool_calls']:.1f}")]


def _experiment_table(exp: str, title: str, arms: list[ArmStats]) -> list[str]:
    control = next((a for a in arms if a.row.role == "control"), arms[0])
    extra = _extra_columns(exp)
    control_cost = control.summary["mean_cost_usd"]
    head = (
        "| Arm | Config | Accuracy | Δ acc | L1 | L2 | L3 | ADV | $/task | Cost vs control | "
        "p50 / p95 latency | " + " | ".join(h for h, _ in extra) + " |"
    )
    lines = [f"### {title}", "", head, "|" + "---|" * (head.count("|") - 1)]
    for a in arms:
        s = a.summary
        is_control = a is control
        lines.append(
            f"| {a.row.arm}{' (control)' if is_control else ''} | {a.row.config} | "
            f"{_pct(s['accuracy'])} | "
            f"{'–' if is_control else _pp(control.summary['accuracy'], s['accuracy'])} | "
            + " | ".join(_pct(a.level_acc(d)) for d in DIFFICULTIES)
            + f" | {_usd(s['mean_cost_usd'])} | "
            + ("1.00×" if is_control else _ratio(control_cost, s["mean_cost_usd"]))
            + f" | {s['latency_p50_s']:.1f}s / {s['latency_p95_s']:.1f}s | "
            + " | ".join(fn(a) for _, fn in extra)
            + " |"
        )
    notes = [f"{a.row.arm}: {a.notes}" for a in arms if a.notes]
    if notes:
        lines += ["", "_Notes: " + "; ".join(notes) + "._"]
    return lines


TITLES = {exp: title for exp, title, _ in PLAN}


def render_summary(rows: list[ManifestRow], header: dict[str, str] | None = None) -> str:
    cache: dict[Path, ArmStats] = {}
    by_exp: dict[str, list[ArmStats]] = {}
    for row in rows:
        if row.run_dir not in cache:
            cache[row.run_dir] = load_arm(row)
        base = cache[row.run_dir]
        by_exp.setdefault(row.experiment, []).append(
            ArmStats(row, base.summary, base.meta, base.results)
        )
    check_same_judge_mode({str(path): arm.meta for path, arm in cache.items()})
    lines = ["# Experiment results", ""]
    for k, v in (header or {}).items():
        lines.append(f"- {k}: {v}")
    first = next(iter(cache.values()), None)
    if first is not None:
        judge = (first.meta.get("config") or {}).get("judge_model") or "off"
        mode = judge_mode(first.meta)
        lines.append(
            f"- git: `{first.meta.get('git_sha')}`; judge: `{judge}`"
            + (f" ({JUDGE_MODE_LABELS[mode]})" if mode else "")
        )
    lines += [
        "- Accuracy is over scored tasks (ungraded and over-budget tasks are excluded); one L3 "
        "or ADV task is 6.25 pp of that level, one L1/L2 task 4.2 pp. Differences of one or two "
        "tasks are within run-to-run noise.",
        "- Cost is estimated from `core/llm.py`'s price table; `Total $` adds the judge.",
        "",
        "## Overview",
        "",
        OVERVIEW_HEAD,
    ]
    seen: set[Path] = set()
    for exp, arms in by_exp.items():
        for a in arms:
            if a.row.run_dir in seen:
                continue
            seen.add(a.row.run_dir)
            lines.append(_overview_row(exp, a))
    for exp, arms in by_exp.items():
        arms = sorted(arms, key=lambda a: a.row.role != "control")  # control first
        lines += ["", *_experiment_table(exp, TITLES.get(exp, exp), arms)]
    total = sum(a.total_usd for a in cache.values())
    lines += ["", f"_Total spend across all runs: {_usd(total)}._", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------

app = typer.Typer(help="Experiment plan: cost estimates and the comparison summary.")


@app.callback()
def _main() -> None:
    """Experiment plan: cost estimates and the comparison summary."""


@app.command()
def estimate(
    difficulty: Annotated[
        list[str] | None, typer.Option("--difficulty", "-d", help="L1/L2/L3/ADV; repeatable")
    ] = None,
    limit: Annotated[int | None, typer.Option(help="stratified sample of N tasks")] = None,
    routing: Annotated[
        bool | None, typer.Option("--routing/--no-routing", help="as `eval run --routing`")
    ] = None,
    semantic_layer: Annotated[
        bool | None,
        typer.Option("--semantic-layer/--no-semantic-layer", help="as `eval run`"),
    ] = None,
    no_judge: Annotated[bool, typer.Option(help="leave out the judge's cost")] = False,
    batch: Annotated[
        bool, typer.Option(help="price the judge at the Message Batches rate (eval run --batch)")
    ] = False,
    factor: Annotated[float, typer.Option(min=0, help="multiply the estimate (cap headroom)")] = 1,
    fmt: Annotated[str, typer.Option("--format", help="usd | json")] = "json",
    benchmarks: Path = DEFAULT_BENCHMARK_ROOT,
) -> None:
    """Estimate one `eval run` under the current AGENT_* environment."""
    settings = Settings()
    update: dict[str, Any] = {}
    if routing is not None:
        update["routing_enabled"] = routing
    if semantic_layer is not None:
        update["semantic_layer_enabled"] = semantic_layer
    if update:
        settings = settings.model_copy(update=update)
    est = estimate_run(
        settings,
        level_counts(benchmarks, difficulty, limit),
        judge=not no_judge,
        batch_judge=batch,
    )
    if fmt == "usd":
        typer.echo(f"{est.total_usd * factor:.2f}")
    elif fmt == "json":
        typer.echo(json.dumps({**est.to_dict(), "factor": factor}, indent=2))
    else:
        raise typer.BadParameter("--format must be usd or json")


@app.command()
def plan(
    cap_factor: Annotated[float, typer.Option(min=0)] = 1.5,
    benchmarks: Path = DEFAULT_BENCHMARK_ROOT,
) -> None:
    """Print the estimate table for the canonical five-experiment plan (all 80 tasks)."""
    typer.echo(plan_table(level_counts(benchmarks), cap_factor))


def read_manifests(paths: list[Path]) -> list[ManifestRow]:
    """Rows of several manifests (e.g. the baseline invocation and the one with the other
    experiments); a later row for the same (experiment, arm) replaces an earlier one."""
    rows: dict[tuple[str, str], ManifestRow] = {}
    for path in paths:
        for row in read_manifest(path):
            rows[(row.experiment, row.arm)] = row
    return list(rows.values())


@app.command()
def summary(
    manifest: Annotated[
        list[Path],
        typer.Argument(
            help="TSV(s) written by scripts/run_experiments.sh; several are merged, later "
            "rows replacing earlier ones for the same experiment/arm"
        ),
    ],
    out: Annotated[Path, typer.Option(help="where to write the Markdown")] = Path(
        "runs/summary.md"
    ),
    agent: Annotated[str | None, typer.Option(help="recorded in the header")] = None,
) -> None:
    """Write the comparison tables for every run listed in MANIFEST."""
    rows = read_manifests(manifest)
    if not rows:
        raise typer.BadParameter(f"{', '.join(map(str, manifest))} lists no runs")
    header = {"manifest": ", ".join(f"`{m}`" for m in manifest)}
    if agent:
        header["agent"] = f"`{agent}`"
    try:
        md = render_summary(rows, header)
    except JudgeModeMismatch as e:
        typer.secho(f"error: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from e
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    typer.echo(md)


if __name__ == "__main__":
    app()
