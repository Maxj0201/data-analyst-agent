from __future__ import annotations

import asyncio
from typing import Any

import pytest

from analyst_agent.eval.agent import AgentInput, AgentOutput
from analyst_agent.eval.judge import JudgeError
from analyst_agent.eval.schema import Expected, Task


class MockJudge:
    """Deterministic stand-in for the LLM judge; records every call."""

    def __init__(
        self, rubric_scores: list[int] | None = None, behavior_pass: bool = True, fail: bool = False
    ):
        self.rubric_scores = rubric_scores
        self.behavior_pass = behavior_pass
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    async def complete(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"system": system, "user": user, "schema": schema})
        await asyncio.sleep(0)
        if self.fail:
            raise JudgeError("boom")
        if "points" in schema["properties"]:
            n = user.split("<rubric>")[1].split("</rubric>")[0].strip().count("\n") + 1
            scores = self.rubric_scores if self.rubric_scores is not None else [1] * n
            return {
                "points": [
                    {"index": i, "score": s, "justification": "mock"} for i, s in enumerate(scores)
                ]
            }
        return {"passed": self.behavior_pass, "justification": "mock"}


@pytest.fixture
def mock_judge() -> MockJudge:
    return MockJudge()


def make_task(difficulty: str = "L1", **expected: Any) -> Task:
    defaults: dict[str, Any] = {
        "L1": {"expected_value": 42.0},
        "L2": {"expected_rows": [{"k": "a", "v": 1.0}]},
        "L3": {"rubric": ["point a", "point b"], "pass_threshold": 0.5},
        "ADV": {"expected_behavior": "be safe"},
    }[difficulty]
    return Task(
        id=f"t_{difficulty.lower()}",
        difficulty=difficulty,
        dataset=["data/x.csv"],
        question="q?",
        expected=Expected(**{**defaults, **expected}),
        tags=["t"],
    )


def out(answer: str = "", **kw: Any) -> AgentOutput:
    return AgentOutput(answer=answer, **kw)


def echo_agent(answer: str = "The answer is 42.") -> Any:
    async def agent(inp: AgentInput) -> AgentOutput:
        return AgentOutput(answer=answer, cost_usd=0.01)

    return agent
