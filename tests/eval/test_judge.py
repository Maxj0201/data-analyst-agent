from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from analyst_agent.eval.judge import JudgeError, LLMJudge, parse_json_object
from analyst_agent.eval.scorers import RUBRIC_SCHEMA


@dataclass
class FakeResponse:
    text: str
    stop_reason: str = "end_turn"


class FakeLLM:
    """Implements the core.llm.LLM protocol shape."""

    def __init__(
        self,
        text: str = '{"passed": true, "justification": "ok"}',
        stop_reason: str = "end_turn",
        exc: Exception | None = None,
    ):
        self.text, self.stop_reason, self.exc = text, stop_reason, exc
        self.calls: list[dict[str, Any]] = []

    def create(self, **kw: Any) -> FakeResponse:
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return FakeResponse(self.text, self.stop_reason)


@pytest.mark.asyncio
async def test_llm_judge_goes_through_llm_protocol() -> None:
    llm = FakeLLM()
    judge = LLMJudge(llm=llm, model="judge-model")
    assert await judge.complete("sys", "user", RUBRIC_SCHEMA) == {
        "passed": True,
        "justification": "ok",
    }
    kw = llm.calls[0]
    assert kw["system"] == "sys" and kw["role"] == "judge" and kw["model"] == "judge-model"
    assert kw["messages"] == [{"role": "user", "content": "user"}]
    assert kw["output_format"] == {"type": "json_schema", "schema": RUBRIC_SCHEMA}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "llm",
    [FakeLLM(stop_reason="refusal"), FakeLLM(text="no json"), FakeLLM(exc=RuntimeError("503"))],
)
async def test_llm_judge_failures_raise_judge_error(llm: FakeLLM) -> None:
    with pytest.raises(JudgeError):
        await LLMJudge(llm=llm).complete("s", "u", {})


def test_parse_json_object_variants() -> None:
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('Sure! {"a": {"b": 2}} done') == {"a": {"b": 2}}
    with pytest.raises(JudgeError):
        parse_json_object("[1, 2]")
