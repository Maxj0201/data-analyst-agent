"""Thin wrapper around ``anthropic.Anthropic().messages.create``.

Responsibilities:
* model / effort / max_tokens from config
* prompt caching (:func:`_build_params`): a breakpoint on the system prompt caches the tool
  schemas and the system prompt (render order is tools -> system -> messages); a caller may end
  a stable leading message block with :func:`cached_text` (the router's schema summary); and
  ``cache_history=True`` (the agent loop) adds a breakpoint on the last block of the
  conversation, so each turn reads the previous turn's prefix instead of paying for the whole
  history again. Nothing per-run (timestamps, run ids) goes into a cached prefix.
* retries with exponential backoff + jitter on transient errors (429, 5xx, network)
* a :class:`CostTracker` that accumulates tokens and estimated USD per run, broken down by role
  and by model (routing sends different steps to different models)

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

# USD per 1M tokens: (input, output). Cache writes (5-minute TTL) bill at 1.25x input, cache
# reads at 0.1x, or less for the models in CACHE_READ_MULT_BY_MODEL.
# Source: https://platform.claude.com/docs/en/about-claude/pricing (checked 2026-11-06).
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
CACHE_READ_MULT_BY_MODEL: dict[str, float] = {
    "claude-fable-5-1": 0.025,
    "claude-opus-5-5": 0.05,
    "claude-sonnet-5-5": 0.05,
}
# Claude Haiku 5.5 is priced by prompt length: a request whose prompt (input + cache writes +
# cache reads) is over 100,000 tokens pays these prices for all of its tokens.
LONG_PROMPT_TOKENS = 100_000
LONG_PROMPT_PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.50, 2.50),
}
# Message Batches API requests bill at half the list price (input, output and cache tokens).
BATCH_DISCOUNT = 0.50

# Prompt caching. 5-minute TTL (the default): eval tasks and agent turns are seconds apart.
EPHEMERAL: dict[str, str] = {"type": "ephemeral"}
MAX_BREAKPOINTS = 4  # API limit per request
# Blocks that cannot carry ``cache_control``.
_UNCACHEABLE_BLOCKS = frozenset({"thinking", "redacted_thinking"})

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that accept the server-side ``fallbacks: "default"`` parameter. Others (e.g. Haiku,
# which a router or a cheap tier may use) are sent a plain request.
FALLBACK_MODELS = frozenset(
    {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
)


def supports_refusal_fallback(model: str) -> bool:
    return model in FALLBACK_MODELS


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


def estimate_cost_usd(
    model: str, usage: Usage, batch: bool = False, *, single_request: bool = True
) -> float:
    """Estimated USD for ``usage``.

    ``single_request=False`` is for usage summed over several requests (planning estimates):
    the long-prompt tier is decided per request, so it is not applied to a total.
    """
    in_price, out_price = PRICING.get(model, DEFAULT_PRICE)
    prompt_tokens = (
        usage.input_tokens + usage.cache_creation_input_tokens + usage.cache_read_input_tokens
    )
    if single_request and model in LONG_PROMPT_PRICING and prompt_tokens > LONG_PROMPT_TOKENS:
        in_price, out_price = LONG_PROMPT_PRICING[model]
    read_mult = CACHE_READ_MULT_BY_MODEL.get(model, CACHE_READ_MULT)
    usd = (
        usage.input_tokens * in_price
        + usage.cache_creation_input_tokens * in_price * CACHE_WRITE_MULT
        + usage.cache_read_input_tokens * in_price * read_mult
        + usage.output_tokens * out_price
    ) / 1_000_000
    return usd * BATCH_DISCOUNT if batch else usd


@dataclass
class ModelUsage:
    """Token and cost totals for one model within a run."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    usd: float = 0.0

    def add(self, usage: Usage, cost: float) -> None:
        self.calls += 1
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_creation_input_tokens += usage.cache_creation_input_tokens
        self.cache_read_input_tokens += usage.cache_read_input_tokens
        self.usd += cost

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
        }


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
    by_model: dict[str, ModelUsage] = field(default_factory=dict)

    def add(self, model: str, usage: Usage, role: str = "agent", batch: bool = False) -> float:
        cost = estimate_cost_usd(model, usage, batch=batch)
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_creation_input_tokens += usage.cache_creation_input_tokens
        self.cache_read_input_tokens += usage.cache_read_input_tokens
        self.usd += cost
        self.calls += 1
        self.by_role[role] = self.by_role.get(role, 0.0) + cost
        self.by_model.setdefault(model, ModelUsage()).add(usage, cost)
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
            "by_model": {k: v.to_dict() for k, v in self.by_model.items()},
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
    """What the loop and critic depend on. Tests provide a scripted fake.

    ``model`` / ``effort`` / ``max_tokens`` override the configured defaults for one call (the
    router uses a small model at low effort; routing picks the working model per tier).
    ``cache_history`` adds a cache breakpoint at the end of ``messages``: for multi-turn callers
    (the agent loop), not for one-off calls whose last message is never sent again.
    """

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
        effort: str | None = None,
        max_tokens: int | None = None,
        cache_history: bool = False,
    ) -> LLMResponse: ...


def cached_text(text: str) -> dict[str, Any]:
    """A text content block that ends a cached prefix (a ``cache_control`` breakpoint).

    Use it for the stable leading part of a user message (e.g. a schema summary that is the same
    for every question on a dataset), followed by the varying part in a plain block.
    """
    return {"type": "text", "text": text, "cache_control": dict(EPHEMERAL)}


def _count_breakpoints(messages: list[dict[str, Any]]) -> int:
    return sum(
        1
        for m in messages
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and "cache_control" in b
    )


def _cacheable(block: Any) -> bool:
    if not isinstance(block, dict) or block.get("type") in _UNCACHEABLE_BLOCKS:
        return False
    return not (block.get("type") == "text" and not block.get("text"))


def cache_conversation_tail(messages: list[dict[str, Any]], used: int = 1) -> list[dict[str, Any]]:
    """``messages`` with a breakpoint on the last cacheable block of the last message.

    Returns a new list; the caller's messages (the loop's history) are never modified, so the
    marker moves forward each turn instead of piling up. The previous turn's breakpoint was on
    a block that is now earlier in the prefix, so this request reads it from the cache and
    writes only the new turn. ``used`` counts breakpoints placed elsewhere (the system prompt);
    the tail is skipped when it would exceed :data:`MAX_BREAKPOINTS`.
    """
    if not messages or used + _count_breakpoints(messages) >= MAX_BREAKPOINTS:
        return messages
    last = messages[-1]
    content = last.get("content")
    if isinstance(content, str):
        if not content:
            return messages
        blocks: list[Any] = [cached_text(content)]
    elif isinstance(content, list):
        blocks = list(content)
        for i in range(len(blocks) - 1, -1, -1):
            if _cacheable(blocks[i]):
                if "cache_control" in blocks[i]:
                    return messages  # already a breakpoint (e.g. a cached_text block)
                blocks[i] = {**blocks[i], "cache_control": dict(EPHEMERAL)}
                break
        else:
            return messages
    else:
        return messages
    return [*messages[:-1], {**last, "content": blocks}]


def _block_to_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return block
    # by_alias: the content is echoed back to the API in the next turn, so fields must use their
    # wire names. A refusal-fallback block's `from` is `from_` on the SDK model; without the alias
    # the echo fails with 400 "messages.N.content.M.fallback.from: Field required".
    return block.model_dump(mode="json", exclude_none=True, by_alias=True)


class BatchUnsupported(RuntimeError):
    """The client cannot send Message Batches (old SDK, or a fake without ``batches``)."""


class BatchRequestError(RuntimeError):
    """One request of a Message Batch did not succeed (errored, canceled or expired)."""


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
        effort: str | None = None,
        max_tokens: int | None = None,
        cache_history: bool = False,
    ) -> dict[str, Any]:
        output_config: dict[str, Any] = {"effort": effort or self.settings.effort}
        if output_format is not None:
            output_config["format"] = output_format
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens or self.settings.max_tokens,
            # Stable system prompt as a cached block: render order is tools -> system -> messages,
            # so this breakpoint caches the (deterministic) tool list and the system prompt.
            "system": [{"type": "text", "text": system, "cache_control": dict(EPHEMERAL)}],
            # The growing conversation: a second breakpoint on its last block (multi-turn only).
            "messages": cache_conversation_tail(messages) if cache_history else messages,
            "output_config": output_config,
        }
        if tools:
            params["tools"] = tools
        return params

    def _send(self, params: dict[str, Any]) -> Any:
        if self.settings.refusal_fallback and supports_refusal_fallback(params["model"]):
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
        effort: str | None = None,
        max_tokens: int | None = None,
        cache_history: bool = False,
    ) -> LLMResponse:
        model = model or self.settings.model
        params = self._build_params(
            system,
            messages,
            tools,
            output_format,
            model,
            effort=effort,
            max_tokens=max_tokens,
            cache_history=cache_history,
        )
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

    # ---- Message Batches ------------------------------------------------------------------

    @property
    def supports_batches(self) -> bool:
        """Whether the SDK client exposes ``messages.batches`` (create / retrieve / results)."""
        batches = getattr(getattr(self.client, "messages", None), "batches", None)
        return all(callable(getattr(batches, m, None)) for m in ("create", "retrieve", "results"))

    def create_batch(
        self,
        requests: list[dict[str, Any]],
        *,
        role: str = "agent",
        cost: CostTracker | None = None,
        poll_interval_s: float = 10.0,
        max_wait_s: float = 6 * 3600.0,
    ) -> list[LLMResponse | Exception]:
        """Send independent single-turn requests as one Message Batch and wait for it.

        Each request takes the keyword arguments of :meth:`create` (``system``, ``messages``,
        ``tools``, ``output_format``, ``model``, ``effort``, ``max_tokens``). Returns one entry
        per request, in order: an :class:`LLMResponse`, or a :class:`BatchRequestError` for a
        request that errored / was canceled / expired. Costs are recorded at the batch price
        (:data:`BATCH_DISCOUNT`).

        Raises :class:`BatchUnsupported` when the client has no batches API, ``TimeoutError``
        (after cancelling the batch) when it has not ended within ``max_wait_s``, and the SDK's
        exception when creating or polling the batch fails, so callers can fall back to
        :meth:`create`. The refusal-fallback beta is not sent in batches.
        """
        if not requests:
            return []
        if not self.supports_batches:
            raise BatchUnsupported("this anthropic SDK client has no messages.batches")
        batches = self.client.messages.batches
        payload = []
        for i, req in enumerate(requests):
            model = req.get("model") or self.settings.model
            params = self._build_params(
                req["system"],
                req["messages"],
                req.get("tools"),
                req.get("output_format"),
                model,
                effort=req.get("effort"),
                max_tokens=req.get("max_tokens"),
            )
            payload.append({"custom_id": f"req-{i}", "params": params})
        batch = batches.create(requests=payload)
        log.info("message batch %s: %d requests submitted", batch.id, len(payload))
        waited = 0.0
        while getattr(batch, "processing_status", "ended") != "ended":
            if waited >= max_wait_s:
                try:
                    batches.cancel(batch.id)
                except Exception:  # noqa: BLE001 - best effort
                    log.warning("could not cancel message batch %s", batch.id)
                raise TimeoutError(f"message batch {batch.id} did not end within {max_wait_s}s")
            self._sleep(poll_interval_s)
            waited += poll_interval_s
            batch = batches.retrieve(batch.id)

        out: list[LLMResponse | Exception] = [
            BatchRequestError("no result returned for this request") for _ in requests
        ]
        for item in batches.results(batch.id):
            try:
                i = int(str(item.custom_id).removeprefix("req-"))
            except ValueError:
                continue
            if not 0 <= i < len(requests):
                continue
            result = item.result
            kind = getattr(result, "type", None)
            if kind != "succeeded":
                err = getattr(getattr(result, "error", None), "error", None)
                msg = getattr(err, "message", None) or kind or "unknown result"
                out[i] = BatchRequestError(f"batch request {kind}: {msg}")
                continue
            raw = result.message
            usage = Usage.from_sdk(raw.usage)
            served_model = getattr(raw, "model", None) or payload[i]["params"]["model"]
            if cost is not None:
                cost.add(served_model, usage, role=role, batch=True)
            out[i] = LLMResponse(
                content=[_block_to_dict(b) for b in raw.content],
                stop_reason=raw.stop_reason,
                usage=usage,
                model=served_model,
            )
        return out
