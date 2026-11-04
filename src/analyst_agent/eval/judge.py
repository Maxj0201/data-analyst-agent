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
import os
import re
from typing import Any, Protocol


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
        if getattr(resp, "stop_reason", None) == "refusal":
            raise JudgeError("judge model refused")
        return parse_json_object(resp.text)

    def cost_summary(self) -> dict[str, Any] | None:
        if self.cost is None:
            return None
        return self.cost.to_dict() if hasattr(self.cost, "to_dict") else {"usd": self.cost}
