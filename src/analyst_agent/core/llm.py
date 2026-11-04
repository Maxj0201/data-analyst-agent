"""Thin wrapper around ``anthropic.Anthropic().messages.create``.

Responsibilities:
* model / effort / max_tokens from config
* the system prompt is sent as a cached block (``cache_control: ephemeral``)
* retries with exponential backoff + jitter on transient errors (429, 5xx, network)
* a :class:`CostTracker` that accumulates tokens and estimated USD per run

Responses are normalised into plain dicts (:class:`LLMResponse`) so the agent loop and the
tests never touch SDK objects directly. Assistant content blocks (including thinking blocks)
are kept intact so they can be passed back unchanged on the next request.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic

from analyst_agent.core.config import Settings, get_settings

log = logging.getLogger(__name__)

# USD per 1M tokens: (input, output). Cache writes bill at 1.25x input, cache reads at 0.1x.
PRICING: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.00, 50.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}
DEFAULT_PRICE = PRICING["claude-opus-5-5"]
CACHE_WRITE_MULT = 1.25
CACHE_READ_MULT = 0.10

FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    @classmethod
    def from_sdk(cls, usage: Any) -> Usage:
        def g(name: str) -> int:
            return int(getattr(usage, name, 0) or 0)

        return cls(
            input_tokens=g("input_tokens"),
            output_tokens=g("output_tokens"),
            cache_creation_input_tokens=g("cache_creation_input_tokens"),
            cache_read_input_tokens=g("cache_read_input_tokens"),
        )


def estimate_cost_usd(model: str, usage: Usage) -> float:
    in_price, out_price = PRICING.get(model, DEFAULT_PRICE)
    return (
        usage.input_tokens * in_price
        + usage.cache_creation_input_tokens * in_price * CACHE_WRITE_MULT
        + usage.cache_read_input_tokens * in_price * CACHE_READ_MULT
        + usage.output_tokens * out_price
    ) / 1_000_000


@dataclass
class CostTracker:
    """Accumulates token usage and estimated cost across every LLM call in a run."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    usd: float = 0.0
    calls: int = 0
    by_role: dict[str, float] = field(default_factory=dict)

    def add(self, model: str, usage: Usage, role: str = "agent") -> float:
        cost = estimate_cost_usd(model, usage)
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_creation_input_tokens += usage.cache_creation_input_tokens
        self.cache_read_input_tokens += usage.cache_read_input_tokens
        self.usd += cost
        self.calls += 1
        self.by_role[role] = self.by_role.get(role, 0.0) + cost
        return cost

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "total_tokens": self.total_tokens,
            "usd": round(self.usd, 6),
            "usd_by_role": {k: round(v, 6) for k, v in self.by_role.items()},
        }

    def summary(self) -> str:
        return (
            f"{self.calls} LLM calls | in {self.input_tokens:,} | out {self.output_tokens:,} | "
            f"cache write {self.cache_creation_input_tokens:,} | "
            f"cache read {self.cache_read_input_tokens:,} | ~${self.usd:.4f}"
        )


@dataclass
class LLMResponse:
    content: list[dict[str, Any]]
    stop_reason: str | None
    usage: Usage
    model: str

    @property
    def text(self) -> str:
        return "\n".join(b.get("text", "") for b in self.content if b.get("type") == "text")

    @property
    def tool_uses(self) -> list[dict[str, Any]]:
        return [b for b in self.content if b.get("type") == "tool_use"]


class LLM(Protocol):
    """What the loop and critic depend on. Tests provide a scripted fake."""

    def create(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        output_format: dict[str, Any] | None = None,
        model: str | None = None,
        role: str = "agent",
        cost: CostTracker | None = None,
    ) -> LLMResponse: ...


def _block_to_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return block
    return block.model_dump(mode="json", exclude_none=True)


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (anthropic.APIConnectionError, anthropic.RateLimitError)):
        return True  # APITimeoutError subclasses APIConnectionError
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code in (408, 409, 429) or exc.status_code >= 500
    return False


class LLMClient:
    """Production :class:`LLM` implementation backed by the Anthropic SDK."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings or get_settings()
        # We own the retry policy, so disable the SDK's built-in retries.
        self.client = client or anthropic.Anthropic(max_retries=0)
        self._sleep = sleep

    def _build_params(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        output_format: dict[str, Any] | None,
        model: str,
    ) -> dict[str, Any]:
        output_config: dict[str, Any] = {"effort": self.settings.effort}
        if output_format is not None:
            output_config["format"] = output_format
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": self.settings.max_tokens,
            # Stable system prompt as a cached block: render order is tools -> system -> messages,
            # so this breakpoint caches the (deterministic) tool list and the system prompt.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
            "output_config": output_config,
        }
        if tools:
            params["tools"] = tools
        return params

    def _send(self, params: dict[str, Any]) -> Any:
        if self.settings.refusal_fallback:
            return self.client.beta.messages.create(
                betas=[FALLBACK_BETA], fallbacks="default", **params
            )
        return self.client.messages.create(**params)

    def create(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        output_format: dict[str, Any] | None = None,
        model: str | None = None,
        role: str = "agent",
        cost: CostTracker | None = None,
    ) -> LLMResponse:
        model = model or self.settings.model
        params = self._build_params(system, messages, tools, output_format, model)
        attempt = 0
        while True:
            try:
                raw = self._send(params)
                break
            except Exception as exc:  # noqa: BLE001 - classified below
                if not _is_retryable(exc) or attempt >= self.settings.max_retries:
                    raise
                delay = min(
                    self.settings.retry_base_delay * (2**attempt),
                    self.settings.retry_max_delay,
                ) + random.uniform(0, self.settings.retry_base_delay)
                log.warning("LLM call failed (%s); retry %d in %.1fs", exc, attempt + 1, delay)
                self._sleep(delay)
                attempt += 1

        usage = Usage.from_sdk(raw.usage)
        served_model = getattr(raw, "model", None) or model
        if cost is not None:
            cost.add(served_model, usage, role=role)
        return LLMResponse(
            content=[_block_to_dict(b) for b in raw.content],
            stop_reason=raw.stop_reason,
            usage=usage,
            model=served_model,
        )
