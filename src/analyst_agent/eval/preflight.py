"""Pre-flight checks before spending money on the benchmark experiments (docs/eval-runbook.md).

``python -m analyst_agent.eval.preflight`` (wrapped by ``scripts/preflight.sh``) checks, in order:

1. ``ANTHROPIC_API_KEY`` (or ``ANTHROPIC_AUTH_TOKEN``) is set in the environment ``eval run``
   will see, and is accepted: one minimal Messages call (1 output token).
2. Every model the runbook uses (agent, critic, router, per-tier and judge models of every
   arm in :data:`analyst_agent.eval.experiments.PLAN`) answers a 1-token call, sent through
   ``core.llm.LLMClient`` exactly like the real run (same effort parameter and refusal-fallback
   beta), plus one small structured-output call to the judge model, parsed like a verdict.
3. Every optional extra of the package is installed (``uv sync --locked --all-extras``). A
   missing ``embeddings`` extra (torch) is only a warning unless embedding backends were asked
   for (``--embeddings``, or ``AGENT_SEMANTIC_BACKEND=embedding|hybrid``): without it the
   semantic experiment's embedding / hybrid arms record a retrieval error on every task.
4. The benchmark data and task files regenerate byte-for-byte from the seeded generator.
5. The governed metric definitions load.
6. The trace store, the eval runs directory and the experiments directory are writable.
7. The estimated cost of every experiment (``core.llm.PRICING``), printed for review.

Exit status: 0 when every check passed, 1 otherwise (warnings do not fail). ``--dry-run``
replaces the API with the scripted :class:`~analyst_agent.core.fake_llm.FakeLLM` (no key, no
network), which is how the tests and a free rehearsal run it. The API checks cost well under
$0.01.
"""

from __future__ import annotations

import contextlib
import filecmp
import importlib.metadata
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer

from analyst_agent.core.config import Settings
from analyst_agent.core.llm import PRICING, CostTracker, LLMResponse, Usage
from analyst_agent.eval.experiments import (
    CONTROL,
    PLAN,
    estimate_run,
    level_counts,
    settings_for,
)
from analyst_agent.eval.judge import CREDENTIAL_ENV_VARS, parse_json_object
from analyst_agent.eval.schema import DEFAULT_BENCHMARK_ROOT

DIST_NAME = "analyst-agent"
GENERATED_DIRS = ("data", "tasks")  # benchmarks/metrics is hand-written

PING_SYSTEM = "Reply with the single word OK."
JUDGE_PROBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    warning: bool = False  # reported, but does not fail the pre-flight

    @property
    def mark(self) -> str:
        if not self.ok:
            return "FAIL"
        return "WARN" if self.warning else "ok"


# --------------------------------------------------------------------------------------------
# Models and API
# --------------------------------------------------------------------------------------------


def runbook_models(judge_model: str | None = None) -> list[str]:
    """Every model id some arm of the plan calls, in first-use order (judge last)."""
    seen: dict[str, None] = {}
    judges: dict[str, None] = {}
    for _exp, _title, arms in PLAN:
        for arm in arms:
            s = settings_for(arm.env)
            seen[s.model] = None
            if s.max_critic_rounds > 0:
                seen[s.resolved_critic_model] = None
            if s.routing_enabled:
                seen[s.router_model] = None
                for tier in ("L1", "L2", "L3"):
                    seen[s.model_for_tier(tier)] = None
            judges[judge_model or s.judge_model or s.model] = None
    for j in judges:
        seen.pop(j, None)
        seen[j] = None
    return list(seen)


def check_api_key(env: dict[str, str] | None = None) -> Check:
    env = dict(os.environ if env is None else env)
    found = [name for name in CREDENTIAL_ENV_VARS if env.get(name)]
    if found:
        return Check("API key set", True, f"{found[0]} is set")
    hint = ""
    if Path(".env").is_file() and re.search(
        r"^\s*ANTHROPIC_API_KEY\s*=", Path(".env").read_text(), re.M
    ):
        hint = " (.env defines it, but only AGENT_* settings are read from .env: export it)"
    return Check(
        "API key set",
        False,
        f"none of {', '.join(CREDENTIAL_ENV_VARS)} is set{hint}; without it eval run skips "
        "every L3 task",
    )


def _error_kind(exc: Exception) -> str:
    """``auth`` (key rejected), ``not_found`` (model unavailable) or ``other``."""
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__
    if status in (401, 403) or name in ("AuthenticationError", "PermissionDeniedError"):
        return "auth"
    if status == 404 or name == "NotFoundError":
        return "not_found"
    return "other"


def check_models(llm: Any, models: list[str], judge_model: str) -> tuple[list[Check], CostTracker]:
    """One 1-token call per model (the first doubles as the key check) and one judge probe."""
    cost = CostTracker()
    checks: list[Check] = []
    key_ok: bool | None = None
    for model in models:
        if key_ok is False:
            checks.append(Check(f"model {model}", False, "not tried: the API key was rejected"))
            continue
        try:
            resp = llm.create(
                system=PING_SYSTEM,
                messages=[{"role": "user", "content": "ping"}],
                model=model,
                role="agent",
                cost=cost,
                effort="low",
                max_tokens=1,
            )
        except Exception as e:  # noqa: BLE001 - every failure is a finding
            kind = _error_kind(e)
            if key_ok is None and kind == "auth":
                key_ok = False
                checks.append(Check("API key valid", False, f"rejected: {type(e).__name__}: {e}"))
                checks.append(Check(f"model {model}", False, "not tried: the API key was rejected"))
                continue
            why = "not available to this key" if kind == "not_found" else "call failed"
            checks.append(Check(f"model {model}", False, f"{why}: {type(e).__name__}: {e}"))
            continue
        if key_ok is None:
            key_ok = True
            checks.append(Check("API key valid", True, f"Messages call to {model} succeeded"))
        served = getattr(resp, "model", model)
        detail = f"stop_reason={resp.stop_reason}" + (
            f", served by {served}" if served != model else ""
        )
        checks.append(Check(f"model {model}", True, detail))
    if key_ok is False:
        return checks, cost
    if key_ok is None and not models:
        checks.append(Check("API key valid", False, "no model to call"))
        return checks, cost
    try:
        resp = llm.create(
            system='Return the JSON object {"ok": true}.',
            messages=[{"role": "user", "content": "ping"}],
            output_format={"type": "json_schema", "schema": JUDGE_PROBE_SCHEMA},
            model=judge_model,
            role="agent",
            cost=cost,
            effort="low",
            max_tokens=32,
        )
        verdict = parse_json_object(resp.text)
        ok = verdict.get("ok") is True
        checks.append(
            Check(
                f"judge structured output ({judge_model})",
                ok,
                "verdict parsed" if ok else f"unexpected verdict {verdict!r}",
            )
        )
    except Exception as e:  # noqa: BLE001 - JudgeError included
        checks.append(
            Check(f"judge structured output ({judge_model})", False, f"{type(e).__name__}: {e}")
        )
    return checks, cost


def check_batches(llm: Any) -> Check:
    if getattr(llm, "supports_batches", False):
        return Check(
            "Message Batches API", True, "available: eval run --batch will batch the judge"
        )
    return Check(
        "Message Batches API",
        True,
        "not available in this client: eval run --batch falls back to direct judge calls",
        warning=True,
    )


# --------------------------------------------------------------------------------------------
# Local checks
# --------------------------------------------------------------------------------------------


def extra_requirements(dist: str = DIST_NAME) -> dict[str, list[str]]:
    """``extra -> [distribution names]`` from the installed package metadata."""
    out: dict[str, list[str]] = {}
    for extra in importlib.metadata.metadata(dist).get_all("Provides-Extra") or []:
        out[extra] = []
    for req in importlib.metadata.requires(dist) or []:
        m = re.search(r"""extra\s*==\s*["']([^"']+)["']""", req)
        if not m:
            continue
        name = re.split(r"[\s;<>=!~\[(]", req, maxsplit=1)[0]
        out.setdefault(m.group(1), []).append(name)
    return out


EMBEDDING_BACKENDS = ("embedding", "hybrid")


def embeddings_requested(
    flag: bool = False, env: dict[str, str] | None = None, settings: Settings | None = None
) -> bool:
    """True when the user asked for embedding backends: ``--embeddings``, or
    ``AGENT_SEMANTIC_BACKEND`` (environment or ``.env``) set to ``embedding`` / ``hybrid``."""
    if flag:
        return True
    env = dict(os.environ if env is None else env)
    backend = env.get("AGENT_SEMANTIC_BACKEND")
    if backend is None:
        backend = (settings or Settings()).semantic_backend
    return backend.strip().lower() in EMBEDDING_BACKENDS


def check_extras(
    extras: dict[str, list[str]] | None = None,
    installed: Callable[[str], bool] | None = None,
    *,
    require_embeddings: bool = True,
) -> list[Check]:
    def _installed(name: str) -> bool:
        try:
            importlib.metadata.version(name)
            return True
        except importlib.metadata.PackageNotFoundError:
            return False

    extras = extra_requirements() if extras is None else extras
    installed = installed or _installed
    checks = []
    for extra, reqs in sorted(extras.items()):
        missing = [r for r in reqs if not installed(r)]
        if missing and extra == "embeddings" and not require_embeddings:
            checks.append(
                Check(
                    f"extra [{extra}]",
                    True,
                    f"missing {', '.join(missing)}: fine unless you run the semantic "
                    "experiment's embedding / hybrid arms (then: uv sync --locked --all-extras, "
                    "and pass --embeddings to make this a failure)",
                    warning=True,
                )
            )
            continue
        checks.append(
            Check(
                f"extra [{extra}]",
                not missing,
                "installed"
                if not missing
                else f"missing {', '.join(missing)}: uv sync --locked --all-extras",
            )
        )
    return checks


def _load_generator(root: Path) -> Any:
    path = root / "data" / "generate.py"
    spec = importlib.util.spec_from_file_location("bench_generate_preflight", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses need the module registered
    spec.loader.exec_module(mod)
    return mod


def _generated_files(root: Path) -> set[Path]:
    return {
        p.relative_to(root)
        for d in GENERATED_DIRS
        if (root / d).is_dir()
        for p in (root / d).rglob("*")
        if p.is_file() and p.suffix in {".csv", ".yaml"}
    }


def check_benchmark_regenerates(root: Path = DEFAULT_BENCHMARK_ROOT) -> Check:
    name = "benchmark regenerates byte-for-byte"
    try:
        gen = _load_generator(root)
        with tempfile.TemporaryDirectory(prefix="preflight-bench-") as tmp:
            out = Path(tmp)
            with contextlib.redirect_stdout(io.StringIO()):  # the generator prints a summary
                gen.generate(out)
            committed, fresh = _generated_files(root), _generated_files(out)
            problems = [f"missing {p}" for p in sorted(committed - fresh)]
            problems += [f"extra {p}" for p in sorted(fresh - committed)]
            problems += [
                f"differs {p}"
                for p in sorted(committed & fresh)
                if not filecmp.cmp(out / p, root / p, shallow=False)
            ]
    except Exception as e:  # noqa: BLE001
        return Check(name, False, f"generator failed: {type(e).__name__}: {e}")
    if problems:
        shown = "; ".join(problems[:5]) + (
            f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""
        )
        return Check(name, False, f"{shown}. Regenerate: python benchmarks/data/generate.py")
    return Check(name, True, f"{len(committed)} files identical")


def check_metrics(metrics_dir: Path) -> Check:
    from analyst_agent.semantic import load_metrics

    try:
        metrics = load_metrics(metrics_dir)
    except Exception as e:  # noqa: BLE001
        return Check("metric definitions load", False, f"{metrics_dir}: {type(e).__name__}: {e}")
    if not metrics:
        return Check("metric definitions load", False, f"{metrics_dir}: no metrics found")
    return Check("metric definitions load", True, f"{len(metrics)} metrics from {metrics_dir}")


def check_writable(dirs: Iterable[tuple[str, Path]]) -> list[Check]:
    checks = []
    for label, d in dirs:
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / f".preflight-{uuid.uuid4().hex}"
            probe.write_text("ok")
            probe.unlink()
            checks.append(Check(f"{label} writable", True, str(d)))
        except OSError as e:
            checks.append(Check(f"{label} writable", False, f"{d}: {e}"))
    return checks


def check_git_clean() -> Check:
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
    except Exception as e:  # noqa: BLE001
        return Check("git tree clean", True, f"could not run git: {e}", warning=True)
    if out.strip():
        n = len(out.strip().splitlines())
        return Check(
            "git tree clean",
            True,
            f"{n} modified tracked file(s): meta.json's git_sha will not describe the code",
            warning=True,
        )
    return Check("git tree clean", True, "meta.json's git_sha will match the code")


# --------------------------------------------------------------------------------------------
# Cost estimate
# --------------------------------------------------------------------------------------------


def cost_table(
    counts: dict[str, int], cap_factor: float = 1.5, batch_judge: bool = False
) -> tuple[str, float]:
    """Per-experiment estimate (the shared Sonnet control counted once, under baseline)."""
    lines = [
        f"| # | Experiment | Arms | Est. $ | Cap $ (×{cap_factor:g}) "
        "| Est. alone incl. control $ |",
        "|---|---|---|---|---|---|",
    ]
    control = estimate_run(settings_for({}), counts, batch_judge=batch_judge).total_usd
    grand = 0.0
    for i, (exp, _title, arms) in enumerate(PLAN, 1):
        total = sum(
            estimate_run(settings_for(a.env), counts, batch_judge=batch_judge).total_usd
            for a in arms
        )
        alone = total if any(a.name == CONTROL for a in arms) else total + control
        names = " + ".join(a.name for a in arms)
        lines.append(
            f"| {i} | {exp} | {names} | ${total:.2f} | ${total * cap_factor:.2f} | ${alone:.2f} |"
        )
        grand += total
    lines.append(f"| | **all** | | **${grand:.2f}** | **${grand * cap_factor:.2f}** | |")
    return "\n".join(lines), grand


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------


def dry_run_llm(models: list[str], judge_model: str) -> Any:
    """A scripted FakeLLM answering every pre-flight call (no network)."""
    from analyst_agent.core.fake_llm import FakeLLM

    usage = Usage(12, 1)
    pings = [LLMResponse([{"type": "text", "text": "OK"}], "max_tokens", usage, m) for m in models]
    probe = LLMResponse(
        [{"type": "text", "text": '{"ok": true}'}], "end_turn", Usage(30, 5), judge_model
    )
    return FakeLLM(agent=[*pings, probe])


def run_preflight(
    *,
    llm: Any | None = None,
    env: dict[str, str] | None = None,
    judge_model: str | None = None,
    limit: int | None = None,
    cap_factor: float = 1.5,
    batch: bool = False,
    benchmarks: Path = DEFAULT_BENCHMARK_ROOT,
    experiments_dir: Path = Path("runs/experiments"),
    skip_api: bool = False,
    skip_regenerate: bool = False,
    extras: dict[str, list[str]] | None = None,
    require_embeddings: bool | None = None,
    dry_run: bool = False,
    echo: Callable[[str], None] = print,
) -> bool:
    """Run every check, print a report; True when nothing failed.

    ``llm`` is the ``core.llm.LLM`` the API checks go through (default: a real
    ``LLMClient`` at effort ``low``); pass a FakeLLM to rehearse offline. ``dry_run`` only
    labels the API checks as rehearsed. ``require_embeddings`` (default: see
    :func:`embeddings_requested`) makes a missing ``embeddings`` extra a failure, not a warning.
    """
    settings = Settings()
    models = runbook_models(judge_model)
    judge = models[-1]
    checks: list[Check] = []
    spend: CostTracker | None = None

    echo(f"Pre-flight for the runbook: models {', '.join(models)} (judge {judge})")
    if skip_api:
        checks.append(Check("API checks", True, "skipped (--skip-api)", warning=True))
    else:
        key = check_api_key(env)
        if llm is None and key.ok:
            from analyst_agent.core.llm import LLMClient

            llm = LLMClient(Settings(effort="low"))
        checks.append(key)
        if llm is not None:
            api_checks, spend = check_models(llm, models, judge)
            checks += api_checks
            checks.append(check_batches(llm))
        if dry_run:  # say so on every API line: no real key was read and no API was called
            for c in checks:
                c.detail = (
                    "not checked: --dry-run uses a placeholder key"
                    if c is key
                    else f"dry run (scripted FakeLLM): {c.detail}"
                )
    if require_embeddings is None:
        require_embeddings = embeddings_requested(settings=settings)
    checks += check_extras(extras, require_embeddings=require_embeddings)
    if skip_regenerate:
        checks.append(Check("benchmark regenerates", True, "skipped", warning=True))
    else:
        checks.append(check_benchmark_regenerates(benchmarks))
    checks.append(check_metrics(settings.resolved_semantic_metrics_dir))
    checks += check_writable(
        [
            ("trace store (AGENT_TRACES_DIR)", settings.traces_dir),
            ("eval runs dir (AGENT_EVAL_RUNS_DIR)", settings.eval_runs_dir),
            ("experiments dir", experiments_dir),
        ]
    )
    checks.append(check_git_clean())

    width = max(len(c.name) for c in checks)
    echo("")
    for c in checks:
        echo(f"  [{c.mark:4}] {c.name:<{width}}  {c.detail}")
    if spend is not None and spend.calls:
        echo(f"  pre-flight API spend: ~${spend.usd:.5f} ({spend.calls} calls)")

    counts = level_counts(benchmarks, limit=limit)
    n = sum(counts.values())
    table, grand = cost_table(counts, cap_factor, batch_judge=batch)
    unpriced = sorted({m for m in models if m not in PRICING})
    echo("")
    echo(
        f"Estimated cost per experiment ({n} tasks: "
        + ", ".join(f"{k} {v}" for k, v in counts.items())
        + f"; judge {'batched, half price' if batch else 'direct'}; core/llm.py PRICING):"
    )
    echo(table)
    if unpriced:
        echo(f"  note: no price for {', '.join(unpriced)}; estimated at the default (Opus) price")
    echo(
        "  Token profiles are planning assumptions (±50%): after the baseline compare with "
        "'Mean cost / task' in its report.md."
    )

    failed = [c for c in checks if not c.ok]
    echo("")
    if failed:
        echo(f"PRE-FLIGHT FAILED: {len(failed)} check(s): " + ", ".join(c.name for c in failed))
    else:
        warned = sum(c.warning for c in checks)
        echo(f"pre-flight passed ({len(checks)} checks, {warned} warning(s)); plan ≈ ${grand:.2f}")
    return not failed


app = typer.Typer(help=__doc__.split("\n")[0] if __doc__ else None, add_completion=False)


@app.command()
def main(
    judge_model: Annotated[
        str | None, typer.Option(help="judge model to check [the runbook's: claude-sonnet-5-5]")
    ] = None,
    limit: Annotated[
        int | None, typer.Option(help="estimate for a stratified sample of N tasks (smoke run)")
    ] = None,
    cap_factor: Annotated[float, typer.Option(min=0, help="cap = estimate × this")] = 1.5,
    batch: Annotated[bool, typer.Option(help="price the judge at the batch rate")] = False,
    dry_run: Annotated[
        bool, typer.Option(help="API checks against the scripted FakeLLM: no key, no network")
    ] = False,
    skip_api: Annotated[bool, typer.Option(help="skip the key and model checks")] = False,
    skip_regenerate: Annotated[bool, typer.Option(help="skip the benchmark regeneration")] = False,
    embeddings: Annotated[
        bool,
        typer.Option(
            "--embeddings",
            help="require the embeddings extra (the semantic experiment's embedding / hybrid "
            "arms); without this a missing extra is a warning, unless AGENT_SEMANTIC_BACKEND "
            "is embedding or hybrid",
        ),
    ] = False,
    experiments_dir: Path = Path("runs/experiments"),
    benchmarks: Path = DEFAULT_BENCHMARK_ROOT,
) -> None:
    """Check key, models, extras, benchmark, metrics and output dirs; print cost estimates."""
    llm = None
    env = None
    if dry_run:
        models = runbook_models(judge_model)
        llm = dry_run_llm(models, models[-1])
        env = {CREDENTIAL_ENV_VARS[0]: "dry-run"}
    ok = run_preflight(
        llm=llm,
        env=env,
        judge_model=judge_model,
        limit=limit,
        cap_factor=cap_factor,
        batch=batch,
        benchmarks=benchmarks,
        experiments_dir=experiments_dir,
        skip_api=skip_api,
        skip_regenerate=skip_regenerate,
        require_embeddings=embeddings_requested(embeddings),
        dry_run=dry_run,
        echo=typer.echo,
    )
    raise typer.Exit(code=0 if ok else 1)


if __name__ == "__main__":
    app()
