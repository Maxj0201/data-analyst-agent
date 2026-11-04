"""Runtime configuration, read from ``AGENT_*`` environment variables (or a ``.env`` file).

This is the single settings class for the whole package: agent core, model routing, semantic
layer, human-in-the-loop approval, the serving layer (``api/``) and the eval harness (``eval/``)
all read from it. Every field is documented in ``docs/configuration.md`` and ``.env.example``;
``tests/test_config.py`` fails if a field is missing from either.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_PACKAGE_DIR = Path(__file__).resolve().parents[1]  # .../analyst_agent


def default_metrics_dir() -> Path:
    """Metric definitions shipped with the installed package, independent of the cwd.

    A built wheel carries them at ``analyst_agent/semantic/metrics`` (hatch ``force-include``
    of ``benchmarks/metrics``); a source checkout (editable install) reads
    ``<repo>/benchmarks/metrics`` next to ``src/``.
    """
    bundled = _PACKAGE_DIR / "semantic" / "metrics"
    if bundled.is_dir():
        return bundled
    return _PACKAGE_DIR.parents[1] / "benchmarks" / "metrics"


class Settings(BaseSettings):
    # An empty value means "unset" (None) for optional settings, e.g. AGENT_JOB_DB_PATH= or
    # AGENT_APPROVAL_ROW_THRESHOLD= (never gate on rows).
    model_config = SettingsConfigDict(
        env_prefix="AGENT_", env_file=".env", extra="ignore", env_parse_none_str=""
    )

    # Models
    model: str = "claude-opus-5-5"
    critic_model: str | None = None  # defaults to ``model`` when unset
    effort: str = "high"  # low | medium | high | xhigh | max
    max_tokens: int = 16000
    # Server-side refusal fallback (beta). Disable for platforms that don't support it.
    # Only sent to models that support it (see ``llm.supports_refusal_fallback``).
    refusal_fallback: bool = True

    # Loop limits
    max_turns: int = 15
    token_budget: int = 400_000  # input + output + cache tokens across the whole run
    max_critic_rounds: int = 2

    # LLM retries
    max_retries: int = 4
    retry_base_delay: float = 1.0
    retry_max_delay: float = 30.0

    # Tools
    artifacts_dir: Path = Path("artifacts")
    max_tool_output_chars: int = 20_000

    # Model routing (core/router.py). Off by default so eval runs can A/B it.
    routing_enabled: bool = False
    router_model: str = "claude-haiku-5-5"
    router_effort: str = "low"
    router_max_tokens: int = 1024
    # Working model per difficulty tier; each defaults to ``model`` when unset.
    model_l1: str | None = None
    model_l2: str | None = None
    model_l3: str | None = None
    # Tier used when the router call fails or returns something unparseable.
    router_fallback_tier: Literal["L1", "L2", "L3"] = "L3"

    # Semantic layer: retrieve governed metric definitions into the planner prompt.
    semantic_layer_enabled: bool = False
    # None = the definitions that ship with the package (see default_metrics_dir()); an
    # explicit relative path resolves against the working directory.
    semantic_metrics_dir: Path | None = None
    semantic_top_k: int = 4

    # Structured final report (core/report.py): enforce the headline / key_numbers / method /
    # caveats / charts schema and flag numbers the tool results don't support to the critic.
    structured_report_enabled: bool = False

    # Cross-session insight memory (core/memory.py), keyed by dataset fingerprint.
    memory_enabled: bool = False
    memory_db_path: Path = Path("data/memory.sqlite3")
    memory_top_k: int = Field(default=3, ge=0)

    # Human-in-the-loop approval (see core/approval.py). Off unless approval_enabled is set.
    approval_enabled: bool = False
    approval_row_threshold: int | None = 1_000_000  # EXPLAIN row estimate; None = never
    approval_python: bool = True  # every run_python call
    approval_budget_fraction: float | None = 0.8  # of token_budget; None = never
    approval_timeout_s: float = 300.0  # no decision within this = denied

    # Serving layer (api/)
    data_dir: Path = Path("data")  # service state; uploads live in <data_dir>/uploads
    job_db_path: Path | None = Path("data/jobs.sqlite3")  # SQLite job store; "" = memory-only
    job_memory_limit: int = Field(default=500, ge=1)  # finished jobs kept in memory
    max_concurrent_jobs: int = Field(default=4, ge=1)
    max_upload_mb: int = Field(default=50, ge=1)
    langfuse_enabled: bool = False  # keys come from the standard LANGFUSE_* env vars
    cors_origins: list[str] = Field(default_factory=list)  # JSON list

    # Observability (core/tracing.py, core/logs.py; docs/observability.md)
    trace_store_enabled: bool = False  # save each run's span tree as <traces_dir>/<id>.json
    traces_dir: Path = Path("traces")
    log_json_enabled: bool = False  # JSON log lines (with run_id/job_id) instead of text
    log_level: str = "INFO"

    # Eval harness (eval/)
    judge_model: str | None = None  # LLM judge; None = ``model``
    judge_effort: str = "low"
    eval_concurrency: int = 4
    eval_timeout_s: float = 300.0
    eval_runs_dir: Path = Path("runs")

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def resolved_semantic_metrics_dir(self) -> Path:
        return self.semantic_metrics_dir or default_metrics_dir()

    @property
    def resolved_critic_model(self) -> str:
        return self.critic_model or self.model

    def model_for_tier(self, tier: str) -> str:
        """Working model for a routed tier (``L1``/``L2``/``L3``), falling back to ``model``."""
        per_tier = {"L1": self.model_l1, "L2": self.model_l2, "L3": self.model_l3}
        if tier not in per_tier:
            raise ValueError(f"unknown tier {tier!r}")
        return per_tier[tier] or self.model


@lru_cache
def get_settings() -> Settings:
    return Settings()
