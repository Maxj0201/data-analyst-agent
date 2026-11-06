from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from analyst_agent.core.config import Settings
from analyst_agent.core.critic import parse_verdict
from analyst_agent.core.llm import FALLBACK_BETA, CostTracker, LLMClient, Usage, estimate_cost_usd

REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def _status_error(cls, code):
    return cls("err", response=httpx2.Response(code, request=REQ), body=None)


def _raw(content=None):
    return SimpleNamespace(
        content=content or [SimpleNamespace(model_dump=lambda **_: {"type": "text", "text": "hi"})],
        stop_reason="end_turn",
        model="claude-opus-5-5",
        usage=SimpleNamespace(
            input_tokens=1000,
            output_tokens=200,
            cache_creation_input_tokens=500,
            cache_read_input_tokens=2000,
        ),
    )


class FakeMessages:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return o


def make_client(outcomes, **settings_kw):
    msgs = FakeMessages(outcomes)
    sdk = SimpleNamespace(messages=msgs, beta=SimpleNamespace(messages=msgs))
    sleeps: list[float] = []
    s = Settings(_env_file=None, retry_base_delay=0.5, **settings_kw)
    return LLMClient(s, client=sdk, sleep=sleeps.append), msgs, sleeps


def test_cost_math():
    u = Usage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_creation_input_tokens=1_000_000,
        cache_read_input_tokens=1_000_000,
    )
    # 4 + 20 + 4*1.25 + 4*0.05 (Opus 5.5 cache reads are 0.05x input)
    assert estimate_cost_usd("claude-opus-5-5", u) == pytest.approx(29.2)
    t = CostTracker()
    t.add("claude-opus-5-5", Usage(input_tokens=10, output_tokens=5), role="agent")
    t.add("claude-opus-5-5", Usage(cache_read_input_tokens=100), role="critic")
    assert t.total_tokens == 115 and t.calls == 2
    assert set(t.to_dict()["usd_by_role"]) == {"agent", "critic"}
    assert "2 LLM calls" in t.summary()


def test_request_shape_and_cost_tracking():
    client, msgs, _ = make_client([_raw()])
    cost = CostTracker()
    out = client.create(
        system="SYS", messages=[{"role": "user", "content": "q"}], tools=[{"name": "t"}], cost=cost
    )
    kw = msgs.calls[0]
    assert kw["model"] == "claude-opus-5-5"
    assert kw["system"] == [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}]
    assert kw["tools"] == [{"name": "t"}]
    assert kw["output_config"] == {"effort": "high"}
    assert kw["betas"] == [FALLBACK_BETA] and kw["fallbacks"] == "default"
    assert out.text == "hi"
    assert cost.input_tokens == 1000 and cost.cache_read_input_tokens == 2000
    assert cost.usd > 0


def test_no_fallback_uses_plain_endpoint_and_output_format():
    client, msgs, _ = make_client([_raw()], refusal_fallback=False)
    fmt = {"type": "json_schema", "schema": {"type": "object"}}
    client.create(system="S", messages=[], output_format=fmt, model="claude-sonnet-5-5")
    kw = msgs.calls[0]
    assert "betas" not in kw and "fallbacks" not in kw
    assert kw["model"] == "claude-sonnet-5-5"
    assert kw["output_config"]["format"] == fmt


def test_retries_transient_errors_with_backoff():
    client, msgs, sleeps = make_client(
        [
            _status_error(anthropic.RateLimitError, 429),
            _status_error(anthropic.InternalServerError, 529),
            anthropic.APIConnectionError(request=REQ),
            _raw(),
        ]
    )
    out = client.create(system="S", messages=[])
    assert out.text == "hi"
    assert len(msgs.calls) == 4 and len(sleeps) == 3
    # exponential: base*1, base*2, base*4 (+ jitter < base)
    assert 0.5 <= sleeps[0] < 1.0 and 1.0 <= sleeps[1] < 1.5 and 2.0 <= sleeps[2] < 2.5


def test_does_not_retry_client_errors():
    client, msgs, sleeps = make_client([_status_error(anthropic.BadRequestError, 400)])
    with pytest.raises(anthropic.BadRequestError):
        client.create(system="S", messages=[])
    assert len(msgs.calls) == 1 and sleeps == []


def test_gives_up_after_max_retries():
    errs = [_status_error(anthropic.RateLimitError, 429) for _ in range(3)]
    client, msgs, sleeps = make_client(errs, max_retries=2)
    with pytest.raises(anthropic.RateLimitError):
        client.create(system="S", messages=[])
    assert len(msgs.calls) == 3 and len(sleeps) == 2


def test_parse_verdict():
    v = parse_verdict('{"verdict": "revise", "issues": ["a"], "suggested_fix": "b"}')
    assert (v.verdict, v.issues, v.suggested_fix, v.parse_error) == ("revise", ["a"], "b", None)
    assert (
        parse_verdict('```json\n{"verdict":"pass","issues":[],"suggested_fix":""}\n```').verdict
        == "pass"
    )
    bad = parse_verdict('{"verdict": "maybe"}')
    assert bad.verdict == "pass" and bad.parse_error


def test_cost_tracker_breaks_down_by_model():
    t = CostTracker()
    # 100k: at Haiku 5.5's long-prompt threshold, so the up-to-100k price applies
    t.add("claude-haiku-5-5", Usage(input_tokens=100_000), role="router")
    t.add("claude-sonnet-5-5", Usage(output_tokens=1_000_000), role="agent")
    t.add("claude-sonnet-5-5", Usage(input_tokens=1_000_000), role="agent")
    d = t.to_dict()["by_model"]
    assert set(d) == {"claude-haiku-5-5", "claude-sonnet-5-5"}
    assert d["claude-haiku-5-5"]["calls"] == 1
    assert d["claude-haiku-5-5"]["usd"] == pytest.approx(0.01)
    assert d["claude-sonnet-5-5"]["calls"] == 2
    assert d["claude-sonnet-5-5"]["input_tokens"] == 1_000_000
    assert d["claude-sonnet-5-5"]["output_tokens"] == 1_000_000
    assert d["claude-sonnet-5-5"]["usd"] == pytest.approx(12.0)
    assert sum(m["usd"] for m in d.values()) == pytest.approx(t.usd)


def test_per_call_overrides_and_fallback_only_for_supported_models():
    client, msgs, _ = make_client([_raw(), _raw()])
    client.create(system="S", messages=[], model="claude-haiku-5-5", effort="low", max_tokens=256)
    haiku = msgs.calls[0]
    assert haiku["model"] == "claude-haiku-5-5"
    assert haiku["output_config"]["effort"] == "low" and haiku["max_tokens"] == 256
    assert "fallbacks" not in haiku and "betas" not in haiku  # Haiku has no server fallback

    client.create(system="S", messages=[])
    opus = msgs.calls[1]
    assert opus["fallbacks"] == "default" and opus["betas"] == [FALLBACK_BETA]
    assert opus["output_config"]["effort"] == client.settings.effort
    assert opus["max_tokens"] == client.settings.max_tokens
