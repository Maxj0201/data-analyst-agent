"""Runtime configuration, read from ``AGENT_*`` environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", env_file=".env", extra="ignore")

    # Models
    model: str = "claude-opus-5-5"
    critic_model: str | None = None  # defaults to ``model`` when unset
    effort: str = "high"  # low | medium | high | xhigh | max
    max_tokens: int = 16000
    # Server-side refusal fallback (beta). Disable for platforms that don't support it.
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

    # Human-in-the-loop approval (see core/approval.py). Off unless approval_enabled is set.
    approval_enabled: bool = False
    approval_row_threshold: int | None = 1_000_000  # EXPLAIN row estimate; None = never
    approval_python: bool = True  # every run_python call
    approval_budget_fraction: float | None = 0.8  # of token_budget; None = never
    approval_timeout_s: float = 300.0  # no decision within this = denied

    @property
    def resolved_critic_model(self) -> str:
        return self.critic_model or self.model


@lru_cache
def get_settings() -> Settings:
    return Settings()
