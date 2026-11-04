"""LLM-as-judge.

Scorers depend only on the :class:`Judge` protocol (``async complete(system, user, schema)``
returning parsed JSON), so tests can plug in a mock without touching the network.

:class:`LLMJudge` is the production judge. Per CLAUDE.md, every LLM call goes through the
single wrapper in ``analyst_agent.core.llm``: the judge takes any object implementing that
module's ``LLM`` protocol (``create(system=..., messages=..., output_format=..., model=...,
role=..., cost=...) -> LLMResponse``) and defaults to ``core.llm.LLMClient``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)


class JudgeError(RuntimeError):
    pass


class JudgeUnavailable(JudgeError):
    """No judge is configured (no credentials). Scorers mark the task skipped, not failed."""


CREDENTIAL_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def judge_configured() -> bool:
    """Whether the default :class:`LLMJudge` has credentials to call the API."""
    return any(os.environ.get(name) for name in CREDENTIAL_ENV_VARS)


class Judge(Protocol):
    async def complete(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        """Return the judge's JSON verdict, validated against ``schema`` where possible."""
        ...


def parse_json_object(text: str) -> dict[str, Any]:
    """Extract the first JSON object from model text (tolerates code fences / prose)."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise JudgeError(f"judge returned no JSON object: {text[:200]!r}") from None
        try:
            obj = json.loads(text[start : end + 1])
        except json.JSONDecodeError as e:
            raise JudgeError(f"judge returned invalid JSON: {e}") from e
    if not isinstance(obj, dict):
        raise JudgeError("judge JSON is not an object")
    return obj


class LLMJudge:
    """Judge that calls the model through ``analyst_agent.core.llm`` with JSON-schema output.

    ``llm`` is any ``core.llm.LLM`` implementation (tests pass a fake). Judge token usage is
    accumulated in ``self.cost`` (a ``core.llm.CostTracker`` when available).
    """

    def __init__(
        self, llm: Any = None, model: str | None = None, effort: str = "low", cost: Any = None
    ):
        self.model = model
        self.effort = effort
        self._llm = llm
        self.cost = cost

    @property
    def llm(self) -> Any:
        if self._llm is None:
            if not judge_configured():
                raise JudgeUnavailable(
                    "no judge configured (set ANTHROPIC_API_KEY, or pass --no-judge)"
                )
            try:
                from analyst_agent.core.config import Settings
                from analyst_agent.core.llm import CostTracker, LLMClient
            except ImportError as e:  # pragma: no cover - until feat/agent-core is merged
                raise JudgeError(
                    "analyst_agent.core.llm is not available; run with --no-judge or merge "
                    "feat/agent-core"
                ) from e
            self._llm = LLMClient(Settings(effort=self.effort))
            if self.cost is None:
                self.cost = CostTracker()
        return self._llm

    def _call(self, system: str, user: str, schema: dict[str, Any]) -> Any:
        return self.llm.create(
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format={"type": "json_schema", "schema": schema},
            model=self.model,
            role="judge",
            cost=self.cost,
        )

    async def complete(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        _ = self.llm  # initialise once on the event-loop thread, not racing in workers
        try:
            resp = await asyncio.to_thread(self._call, system, user, schema)
        except JudgeError:
            raise
        except Exception as e:  # API failures after the wrapper's retries
            raise JudgeError(f"judge call failed: {type(e).__name__}: {e}") from e
        return self.parse_response(resp)

    @staticmethod
    def parse_response(resp: Any) -> dict[str, Any]:
        if getattr(resp, "stop_reason", None) == "refusal":
            raise JudgeError("judge model refused")
        return parse_json_object(resp.text)

    def request(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        """The keyword arguments of one judge call, as ``LLMClient.create_batch`` takes them."""
        return {
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_format": {"type": "json_schema", "schema": schema},
            "model": self.model,
        }

    def cost_summary(self) -> dict[str, Any] | None:
        if self.cost is None:
            return None
        return self.cost.to_dict() if hasattr(self.cost, "to_dict") else {"usd": self.cost}


@dataclass
class _Pending:
    system: str
    user: str
    schema: dict[str, Any]
    future: asyncio.Future[dict[str, Any]]


@dataclass
class BatchStats:
    batches: int = 0  # Message Batches sent
    batched_requests: int = 0  # judge calls answered by a batch
    direct_requests: int = 0  # judge calls sent one by one (fallback)
    fallback: str | None = None  # why batching was not (or not always) used

    def to_dict(self) -> dict[str, Any]:
        return {
            "batches": self.batches,
            "batched_requests": self.batched_requests,
            "direct_requests": self.direct_requests,
            "fallback": self.fallback,
        }


@dataclass
class BatchJudge:
    """A :class:`Judge` that queues calls and sends them together as one Message Batch.

    ``complete()`` only enqueues and waits; the runner calls :meth:`flush` once every
    unfinished task of the run is waiting on the judge (``on_enqueue`` tells it when a call
    was queued), so a run normally sends a single batch with every L3/ADV verdict at half the
    list price. When the wrapped LLM cannot send batches (an SDK without ``messages.batches``,
    a test fake) or the batch fails, the queued calls are sent one by one through
    ``LLMJudge.complete`` instead: silently, apart from a log line and ``stats.fallback``.
    """

    judge: LLMJudge
    poll_interval_s: float = 10.0
    max_wait_s: float = 6 * 3600.0
    on_enqueue: Callable[[], None] | None = None
    stats: BatchStats = field(default_factory=BatchStats)
    _queue: list[_Pending] = field(default_factory=list)

    @property
    def cost(self) -> Any:
        return self.judge.cost

    @property
    def pending(self) -> int:
        return len(self._queue)

    def cost_summary(self) -> dict[str, Any] | None:
        return self.judge.cost_summary()

    async def complete(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._queue.append(_Pending(system, user, schema, fut))
        if self.on_enqueue is not None:
            self.on_enqueue()
        return await fut

    async def flush(self) -> None:
        """Answer every queued call (one batch, or one by one as a fallback). Never raises."""
        items, self._queue = self._queue, []
        if not items:
            return
        try:
            await self._flush(items)
        except Exception as e:  # noqa: BLE001 - a waiting scorer must never hang
            log.exception("judge flush failed")
            for it in items:
                if not it.future.done():
                    it.future.set_exception(JudgeError(f"judge batch failed: {e}"))

    async def _flush(self, items: list[_Pending]) -> None:
        try:
            llm = self.judge.llm
        except JudgeError as e:  # JudgeUnavailable included: scorers mark the task skipped
            for it in items:
                it.future.set_exception(e)
            return
        responses: list[Any] | None = None
        if callable(getattr(llm, "create_batch", None)) and getattr(llm, "supports_batches", False):
            try:
                responses = await asyncio.to_thread(
                    llm.create_batch,
                    [self.judge.request(it.system, it.user, it.schema) for it in items],
                    role="judge",
                    cost=self.judge.cost,
                    poll_interval_s=self.poll_interval_s,
                    max_wait_s=self.max_wait_s,
                )
                self.stats.batches += 1
            except Exception as e:  # noqa: BLE001 - fall back to direct calls
                self.stats.fallback = f"batch failed: {type(e).__name__}: {e}"
                log.info("judge batch failed (%s); sending the calls one by one", e)
        elif self.stats.fallback is None:
            self.stats.fallback = "the LLM client does not support message batches"
            log.info("judge: message batches unsupported; sending the calls one by one")

        retry: list[_Pending] = []
        if responses is None:
            retry = items
        else:
            for it, resp in zip(items, responses, strict=True):
                if isinstance(resp, Exception):
                    retry.append(it)  # errored / expired inside the batch: one direct retry
                    continue
                self.stats.batched_requests += 1
                try:
                    it.future.set_result(self.judge.parse_response(resp))
                except JudgeError as e:
                    it.future.set_exception(e)
        if retry:
            self.stats.direct_requests += len(retry)
            await asyncio.gather(*(self._direct(it) for it in retry))

    async def _direct(self, it: _Pending) -> None:
        try:
            it.future.set_result(await self.judge.complete(it.system, it.user, it.schema))
        except Exception as e:  # noqa: BLE001 - delivered to the waiting scorer
            it.future.set_exception(e if isinstance(e, JudgeError) else JudgeError(str(e)))
