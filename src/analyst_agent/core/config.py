"""Runtime configuration, read from ``AGENT_*`` environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", env_file=".env", extra="ignore")

    # Models
    model: str = "claude-opus-5-5"
    critic_model: str | None = None  # defaults to ``model`` when unset
    effort: str = "high"  # low | medium | high | xhigh | max
    max_tokens: int = 16000
    # Server-side refusal fallback (beta). Disable for platforms that don't support it.
    # Only sent to models that support it (see ``llm.supports_refusal_fallback``).
    refusal_fallback: bool = True

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

    # Semantic layer: retrieve governed metric definitions into the planner prompt.
    semantic_layer_enabled: bool = False
    semantic_metrics_dir: Path = Path("benchmarks/metrics")
    semantic_top_k: int = 4

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
