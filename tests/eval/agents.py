"""Module-level (hence picklable) agents for tests that run tasks in child processes."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from analyst_agent.core.fake_llm import FakeLLM, resp, text, tool_use
from analyst_agent.core.llm import CostTracker, LLMResponse
from analyst_agent.eval.agent import AgentInput, AgentOutput, _run_core, output_from_core


class SleepingLLM(FakeLLM):
    """FakeLLM whose second agent call hangs, like an API request that never returns.

    Every call appends ``<pid> <call number>`` to ``log``, so a test can check both that the
    process was killed and that no LLM call happened after the timeout.
    """

    def __init__(self, log: Path, sleep_s: float) -> None:
        super().__init__(
            agent=[
                resp(text("<plan>look</plan>"), tool_use("run_sql", query="SELECT 1 AS x")),
                resp(text("never returned")),
            ]
        )
        self.log, self.sleep_s = log, sleep_s

    def create(
        self, *, role: str = "agent", cost: CostTracker | None = None, **kw: Any
    ) -> LLMResponse:
        with open(self.log, "a") as f:
            f.write(f"{os.getpid()} {len(self.calls) + 1}\n")
        if len(self.calls) >= 1:
            time.sleep(self.sleep_s)
        return super().create(role=role, cost=cost, **kw)


class SleepingCoreAgent:
    """The real core agent loop + sandbox, driven by :class:`SleepingLLM`."""

    def __init__(self, log: Path, sleep_s: float = 60.0) -> None:
        self.log, self.sleep_s = log, sleep_s

    def __call__(self, inp: AgentInput) -> AgentOutput:  # sync, like the worst-case agent
        llm = SleepingLLM(self.log, self.sleep_s)
        result = _run_core(inp.question, list(inp.dataset_paths), lambda q, sb: llm)
        return output_from_core(result)


def raising_agent(inp: AgentInput) -> AgentOutput:
    raise ValueError("kaput")


def crashing_agent(inp: AgentInput) -> AgentOutput:
    os._exit(3)


async def echo_42(inp: AgentInput) -> AgentOutput:
    return AgentOutput(answer="The answer is 42.", cost_usd=0.01)
