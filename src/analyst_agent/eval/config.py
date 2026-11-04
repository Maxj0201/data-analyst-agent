"""Eval settings, read from ``AGENT_*`` environment variables (pydantic-settings)."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class EvalSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", env_file=".env", extra="ignore")

    # LLM judge; ``None`` means the core default model (AGENT_MODEL)
    judge_model: str | None = None
    judge_effort: str = "low"
    # runner defaults
    eval_concurrency: int = 4
    eval_timeout_s: float = 300.0
    eval_runs_dir: Path = Path("runs")
