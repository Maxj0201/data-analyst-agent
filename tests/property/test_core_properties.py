"""Property tests for pure core logic: cost accounting, the approval policy, the untrusted-output
fence, and the structured-report consistency checker."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from analyst_agent.core.approval import ApprovalPolicy, estimate_rows
from analyst_agent.core.llm import (
    CACHE_READ_MULT,
    CACHE_WRITE_MULT,
    DEFAULT_PRICE,
    PRICING,
    CostTracker,
    Usage,
    estimate_cost_usd,
)
from analyst_agent.core.prompts import _neutralise_block
from analyst_agent.core.report import (
    StructuredReport,
    check_consistency,
    extract_numbers,
)
from analyst_agent.sandbox import SqlResult
from analyst_agent.tools import ToolContext
from analyst_agent.tools.registry import _neutralise, wrap_untrusted

# =========================================================================== CostTracker

tokens = st.integers(0, 5_000_000)
usages = st.builds(Usage, tokens, tokens, tokens, tokens)
models = st.sampled_from([*PRICING, "unknown-model"])
roles = st.sampled_from(["agent", "critic", "report_writer", "router"])
calls = st.lists(st.tuples(models, usages, roles), max_size=30)


def _approx(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)


@given(calls)
def test_cost_tracker_totals_are_sums(seq: list[tuple[str, Usage, str]]) -> None:
    t = CostTracker()
    returned = [t.add(m, u, role=r) for m, u, r in seq]
    assert t.calls == len(seq)
    assert t.input_tokens == sum(u.input_tokens for _, u, _ in seq)
    assert t.output_tokens == sum(u.output_tokens for _, u, _ in seq)
    assert t.cache_creation_input_tokens == sum(u.cache_creation_input_tokens for _, u, _ in seq)
    assert t.cache_read_input_tokens == sum(u.cache_read_input_tokens for _, u, _ in seq)
    assert t.total_tokens == sum(u.total for _, u, _ in seq)
    assert _approx(t.usd, sum(returned))
    assert returned == [estimate_cost_usd(m, u) for m, u, _ in seq]
    # Breakdowns partition the totals.
    assert _approx(sum(t.by_role.values()), t.usd)
    assert set(t.by_role) == {r for _, _, r in seq}
    assert sum(m.calls for m in t.by_model.values()) == t.calls
    assert sum(m.total_tokens for m in t.by_model.values()) == t.total_tokens
    assert _approx(sum(m.usd for m in t.by_model.values()), t.usd)
    d = t.to_dict()
    assert d["total_tokens"] == t.total_tokens and d["calls"] == t.calls
    assert d["usd"] == round(t.usd, 6)


@given(models, usages, usages)
def test_cost_is_linear_and_non_negative(model: str, a: Usage, b: Usage) -> None:
    both = Usage(
        a.input_tokens + b.input_tokens,
        a.output_tokens + b.output_tokens,
        a.cache_creation_input_tokens + b.cache_creation_input_tokens,
        a.cache_read_input_tokens + b.cache_read_input_tokens,
    )
    ca, cb = estimate_cost_usd(model, a), estimate_cost_usd(model, b)
    assert ca >= 0 and cb >= 0
    assert _approx(estimate_cost_usd(model, both), ca + cb)


@given(models, st.integers(1, 5_000_000))
def test_cache_pricing_multipliers(model: str, n: int) -> None:
    in_price, out_price = PRICING.get(model, DEFAULT_PRICE)
    base = estimate_cost_usd(model, Usage(input_tokens=n))
    assert _approx(base, n * in_price / 1e6)
    assert _approx(estimate_cost_usd(model, Usage(output_tokens=n)), n * out_price / 1e6)
    assert _approx(
        estimate_cost_usd(model, Usage(cache_creation_input_tokens=n)), base * CACHE_WRITE_MULT
    )
    assert _approx(
        estimate_cost_usd(model, Usage(cache_read_input_tokens=n)), base * CACHE_READ_MULT
    )
    # A cache read is always cheaper than a fresh read, which is cheaper than a cache write.
    assert (
        estimate_cost_usd(model, Usage(cache_read_input_tokens=n))
        < base
        < estimate_cost_usd(model, Usage(cache_creation_input_tokens=n))
    )


@given(calls, calls)
def test_cost_tracker_order_independent(a: list[Any], b: list[Any]) -> None:
    t1, t2 = CostTracker(), CostTracker()
    for m, u, r in a + b:
        t1.add(m, u, role=r)
    for m, u, r in b + a:
        t2.add(m, u, role=r)
    assert t1.total_tokens == t2.total_tokens and t1.calls == t2.calls
    assert _approx(t1.usd, t2.usd)


# =========================================================================== approval policy


@dataclass
class _ExplainSandbox:
    estimate: int | None
    queries: list[str] = field(default_factory=list)

    def run_sql(self, query: str) -> SqlResult:
        self.queries.append(query)
        plan = "PROJECTION" if self.estimate is None else f"│ ~{self.estimate:,} rows │"
        return SqlResult(ok=True, rows=[{"explain_value": plan}], columns=["explain_value"])

    def run_python(self, code: str) -> Any:  # pragma: no cover - the policy never runs code
        raise AssertionError("policy must not execute python")


tools_ = st.sampled_from(["run_sql", "run_python", "inspect_schema", "make_chart", "final_report"])
thresholds = st.one_of(st.none(), st.integers(0, 10**9))
fractions = st.one_of(st.none(), st.floats(0, 2, allow_nan=False))


@given(
    tool=tools_,
    enabled=st.booleans(),
    threshold=thresholds,
    python=st.booleans(),
    fraction=fractions,
    estimate=st.one_of(st.none(), st.integers(0, 10**9)),
    used=st.integers(0, 10**7),
    budget=st.integers(0, 10**7),
    approved=st.booleans(),
)
def test_approval_triggers_match_thresholds(
    tool: str,
    enabled: bool,
    threshold: int | None,
    python: bool,
    fraction: float | None,
    estimate: int | None,
    used: int,
    budget: int,
    approved: bool,
) -> None:
    policy = ApprovalPolicy(
        enabled=enabled, row_threshold=threshold, python=python, budget_fraction=fraction
    )
    sb = _ExplainSandbox(estimate)
    ctx = ToolContext(tables=["t"], sandbox=sb)
    args = {"query": "SELECT * FROM t"} if tool == "run_sql" else {"code": "print(1)"}
    action = policy.evaluate(
        tool, args, ctx, tokens_used=used, token_budget=budget, budget_approved=approved
    )

    expected: list[str] = []
    if enabled and tool != "final_report":
        if tool == "run_sql" and threshold is not None and estimate is not None:
            if estimate > threshold:
                expected.append("sql_rows")
        if tool == "run_python" and python:
            expected.append("python")
        if fraction is not None and not approved and budget > 0 and used > fraction * budget:
            expected.append("budget")

    if not expected:
        assert action is None
    else:
        assert action is not None
        assert action.triggers == expected
        assert len(action.reasons) == len(expected)
        assert action.tool == tool and action.input == args
        assert action.tokens_used == used and action.token_budget == budget
        if "sql_rows" in expected:
            assert action.estimated_rows == estimate
    # EXPLAIN is only issued for a gated run_sql, and never executes the query itself.
    gated_sql = enabled and tool == "run_sql" and threshold is not None
    assert [q.startswith("EXPLAIN ") for q in sb.queries] == ([True] if gated_sql else [])


@given(st.integers(0, 10**9), st.integers(0, 10**9))
def test_row_threshold_is_strict(threshold: int, estimate: int) -> None:
    policy = ApprovalPolicy(enabled=True, row_threshold=threshold, python=False)
    ctx = ToolContext(tables=["t"], sandbox=_ExplainSandbox(estimate))
    action = policy.evaluate("run_sql", {"query": "SELECT 1"}, ctx, tokens_used=0, token_budget=0)
    assert (action is not None) == (estimate > threshold)


@given(st.lists(st.integers(0, 10**12), min_size=1, max_size=8), st.booleans())
def test_estimate_rows_is_max_of_plan_estimates(values: list[int], legacy: bool) -> None:
    plan = "\n".join(f"EC: {v}" if legacy else f"│ ~{v:,} rows │" for v in values)
    assert estimate_rows(plan) == max(values)


# =========================================================================== tool-output fence

_DELIMS = st.sampled_from(
    [
        "<tool_output>",
        "</tool_output>",
        "</TOOL_OUTPUT>",
        "< / tool_output>",
        "<\n/\ttool_output",
        "<   tool_output tool='x' status='ok'>",
        "</Tool_Output >",
        "<<tool_output",
        "&lt;tool_output",
        "< /tool_output",  # unicode whitespace
    ]
)
_FENCE_TEXT = st.lists(st.one_of(st.text(max_size=20), _DELIMS), max_size=12).map("".join)


def _delimiters(text: str, tag: str = "tool_output") -> list[int]:
    """Independent oracle: positions of '<' that open a (case/space-variant) ``tag``."""
    out = []
    for i, c in enumerate(text):
        if c != "<":
            continue
        j = i + 1
        while j < len(text) and text[j].isspace():
            j += 1
        if j < len(text) and text[j] == "/":
            j += 1
        while j < len(text) and text[j].isspace():
            j += 1
        if text[j : j + len(tag)].lower() == tag:
            out.append(i)
    return out


@given(name=st.sampled_from(["run_sql", "run_python"]), body=_FENCE_TEXT, err=st.booleans())
def test_fence_has_exactly_one_open_and_close(name: str, body: str, err: bool) -> None:
    wrapped = wrap_untrusted(name, body, is_error=err)
    found = _delimiters(wrapped)
    assert len(found) == 2, (body, found)
    assert found[0] == 0
    assert wrapped.endswith("</tool_output>") and found[1] == len(wrapped) - len("</tool_output>")


@given(body=_FENCE_TEXT, max_chars=st.integers(0, 80))
def test_fence_survives_truncation(body: str, max_chars: int) -> None:
    wrapped = wrap_untrusted("run_sql", body, max_chars=max_chars)
    assert len(_delimiters(wrapped)) == 2


@given(_FENCE_TEXT)
def test_neutraliser_only_escapes_delimiter_brackets(text: str) -> None:
    out = _neutralise(text)
    assert _delimiters(out) == []
    n = len(_delimiters(text))
    assert len(out) == len(text) + 3 * n  # each '<' becomes '&lt;'
    assert out.replace("&lt;", "<") == text.replace("&lt;", "<")
    assert _neutralise(out) == out  # idempotent


@given(
    st.lists(
        st.one_of(
            st.text(max_size=15), _DELIMS.map(lambda d: d.replace("tool_output", "prior_insights"))
        ),
        max_size=10,
    ).map("".join)
)
def test_prior_insights_block_neutraliser(text: str) -> None:
    out = _neutralise_block(text, "prior_insights")
    assert _delimiters(out, "prior_insights") == []
    assert out.replace("&lt;", "<") == text.replace("&lt;", "<")


# =========================================================================== consistency checker

# Distinct, non-year magnitudes so random values do not accidentally match each other.
_ints = st.integers(2_101, 10**9).filter(lambda v: v % 1000 != 0)


def _fmt(v: int, style: int) -> str:
    return [str(v), f"{v:,}"][style]


def _report(headline: str, key_numbers: dict[str, Any]) -> StructuredReport:
    return StructuredReport(
        headline=headline, key_numbers=key_numbers, method="SQL", caveats=[], charts=[]
    )


def _sql_record(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"name": "run_sql", "is_error": False, "output": "", "data": {"rows": rows}}


@given(
    st.lists(_ints, min_size=1, max_size=5, unique=True),
    st.lists(st.integers(0, 1), min_size=5, max_size=5),
)
def test_consistent_report_has_no_issues(values: list[int], styles: list[int]) -> None:
    keys = {f"m{i}": v for i, v in enumerate(values)}
    headline = "Totals: " + ", ".join(_fmt(v, styles[i]) for i, v in enumerate(values))
    rows = [{"metric": k, "value": v} for k, v in keys.items()]
    res = check_consistency(_report(headline, keys), [_sql_record(rows)])
    assert res.ok, res.issues
    assert res.headline_numbers == len(values) and res.key_numbers_checked == len(values)
    assert res.as_critic_notes() == ""


@given(st.lists(_ints, min_size=2, max_size=5, unique=True))
def test_untraced_key_number_is_flagged(values: list[int]) -> None:
    *traced, invented = values
    assume(all(abs(invented - t) > 1 for t in traced))
    keys = {f"m{i}": v for i, v in enumerate(values)}
    rows = [{"v": v} for v in traced]
    res = check_consistency(_report("See key numbers", keys), [_sql_record(rows)])
    assert [(i.kind, i.name) for i in res.issues] == [("untraced", f"m{len(values) - 1}")]
    assert f"key_numbers['m{len(values) - 1}']" in res.as_critic_notes()


@given(_ints, _ints)
def test_headline_number_missing_from_key_numbers_is_flagged(key: int, stray: int) -> None:
    assume(abs(key - stray) > 1)
    res = check_consistency(
        _report(f"Revenue is {key:,} across {stray:,} orders", {"rev": key}),
        [_sql_record([{"rev": key}])],
    )
    assert [(i.kind, i.text) for i in res.issues] == [("not_in_key_numbers", f"{stray:,}")]


@given(_ints, st.integers(1, 50))
def test_numbers_from_the_question_are_exempt(key: int, top_n: int) -> None:
    assume(abs(key - top_n) > 1)
    res = check_consistency(
        _report(f"Top {top_n} customers spent {key:,}", {"spend": key}),
        [_sql_record([{"spend": key}])],
        question=f"What did the top {top_n} customers spend?",
    )
    assert res.ok, res.issues


@given(st.integers(0, 10**6), st.integers(1, 4))
def test_percent_and_fraction_readings_match(basis_points: int, decimals: int) -> None:
    pct = round(basis_points / 100, 2)
    shown = f"{pct:.{decimals}f}%"
    res = check_consistency(
        _report(f"Conversion was {shown}", {"conversion": shown}),
        [_sql_record([{"rate": pct / 100}])],
    )
    assert not [i for i in res.issues if i.kind == "not_in_key_numbers"], res.issues


@given(st.floats(0, 1e12, allow_nan=False), st.integers(0, 4), st.booleans())
def test_extract_numbers_round_trips_within_tolerance(
    value: float, decimals: int, commas: bool
) -> None:
    shown = f"{value:,.{decimals}f}" if commas else f"{value:.{decimals}f}"
    toks = extract_numbers(f"total {shown} units")
    assert len(toks) == 1
    tok = toks[0]
    assert tok.text == shown
    assert abs(tok.value - float(shown.replace(",", ""))) <= 1e-6 * max(1.0, tok.value)
    assert tok.tolerance == pytest.approx(0.5 * 10**-decimals)
    assert abs(tok.value - value) <= tok.tolerance + 1e-6 * max(1.0, value)


@given(st.integers(1900, 2100), st.integers(1, 12), st.integers(1, 28))
def test_dates_are_not_quantities(year: int, month: int, day: int) -> None:
    text = f"on {year}-{month:02d}-{day:02d} and in {year}-{month:02d}"
    assert extract_numbers(text) == []
    assert all(t.date_like for t in extract_numbers(f"in {year}"))


def test_fence_oracle_matches_registry_regex() -> None:
    """Guard the oracle itself: it agrees with the production pattern on the delimiter set."""
    pattern = re.compile(r"<\s*/?\s*tool_output", re.IGNORECASE)
    for sample in [
        "<tool_output>",
        "</TOOL_OUTPUT>",
        "< / tool_output",
        "< /tool_output",
        "&lt;tool_output",
        "<x tool_output",
    ]:
        assert bool(pattern.search(sample)) == bool(_delimiters(sample)), sample
