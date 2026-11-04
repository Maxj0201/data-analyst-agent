"""make_chart: chart-type inference from data shape, defaults and validation."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from analyst_agent.tools import ToolContext, build_default_registry
from analyst_agent.tools.make_chart import infer_chart_type


@pytest.mark.parametrize(
    ("x", "kind"),
    [
        (["2025-01", "2025-02", "2025-03"], "line"),
        (["2025-01-01", "2025-01-02"], "line"),
        (["2024-Q1", "2024-Q2"], "line"),
        (["Jan 2025", "Feb 2025"], "line"),
        ([date(2025, 1, 1), date(2025, 2, 1)], "line"),
        ([2021, 2022, 2023], "line"),
        (["2021", "2022"], "line"),
        ([1, 2, 3, 4], "line"),
        ([3, 1, 2], "scatter"),
        (["north", "south", "east"], "bar"),
        ([f"cat{i}" for i in range(20)], "barh"),
        (["a very long category label indeed", "short"], "barh"),
        (None, "hist"),
        ([], "hist"),
    ],
)
def test_infer_chart_type(x: list[Any] | None, kind: str) -> None:
    got, reason = infer_chart_type(x, [1] * len(x or [1]))
    assert got == kind and reason


def _chart(ctx: ToolContext, **spec: Any) -> Any:
    return build_default_registry().execute("make_chart", {"spec": spec}, ctx)


def test_auto_time_series_renders_line(ctx: ToolContext) -> None:
    res = _chart(ctx, title="Monthly revenue", x=["2025-01", "2025-02", "2025-03"], y=[10, 12, 9])
    assert not res.is_error, res.content
    assert res.data["chart_type"] == "line" and "time series" in res.content
    assert Path(res.data["path"]).exists()


def test_auto_category_compare_renders_bar(ctx: ToolContext) -> None:
    res = _chart(ctx, x=["north", "south"], y=[150, 200], x_label="region", y_label="revenue")
    assert not res.is_error and res.data["chart_type"] == "bar"
    assert res.data["title"] == "revenue by region"  # default title from axis labels


def test_auto_distribution_renders_hist(ctx: ToolContext) -> None:
    res = _chart(ctx, y=[1, 2, 2, 3, 3, 3, None], y_label="order value")
    assert not res.is_error and res.data["chart_type"] == "hist"
    assert res.data["title"] == "Distribution of order value"


def test_auto_many_categories_renders_horizontal_bars(ctx: ToolContext) -> None:
    cats = [f"product {i}" for i in range(25)]
    res = _chart(ctx, title="Units", x=cats, y=list(range(25)))
    assert not res.is_error and res.data["chart_type"] == "barh"


def test_explicit_chart_type_wins(ctx: ToolContext) -> None:
    res = _chart(ctx, chart_type="bar", title="t", x=["2025-01", "2025-02"], y=[1, 2])
    assert not res.is_error and res.data["chart_type"] == "bar"
    assert "chart_type=" not in res.content  # the reason is only reported when inferred


def test_multi_series_auto(ctx: ToolContext) -> None:
    res = _chart(
        ctx,
        title="Revenue by region and year",
        series=[
            {"name": "2024", "x": ["north", "south"], "y": [1, 2]},
            {"name": "2025", "x": ["north", "south"], "y": [2, 3]},
        ],
    )
    assert not res.is_error and res.data["chart_type"] == "bar"


@pytest.mark.parametrize(
    "spec",
    [
        {"chart_type": "radar", "x": [1], "y": [1]},
        {"chart_type": "bar", "x": [1], "y": [1, 2]},
        {"chart_type": "bar", "y": [1, 2]},
        {"x": ["a"], "y": []},
        {"x": ["a"], "y": ["not a number"]},
        {"series": "nope"},
    ],
)
def test_invalid_specs_are_tool_errors(ctx: ToolContext, spec: dict[str, Any]) -> None:
    assert _chart(ctx, **spec).is_error
