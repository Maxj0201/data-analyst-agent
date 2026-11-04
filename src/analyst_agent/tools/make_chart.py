"""``make_chart``: render a PNG from data the agent already computed.

``chart_type`` is optional. When it is omitted (or ``"auto"``), :func:`infer_chart_type` picks
it from the shape of the data:

* only ``y`` values (no ``x``)                    -> ``hist`` (a distribution)
* ``x`` values that are dates / periods / years   -> ``line`` (a time series)
* numeric, strictly increasing ``x``              -> ``line``
* other numeric ``x``                             -> ``scatter``
* categorical ``x``                               -> ``bar``, or ``barh`` when there are many
                                                     categories or long labels

Rendering uses Matplotlib's object-oriented API only (``Figure`` + ``FigureCanvasAgg``): pyplot
keeps global figure state, which is not safe when several API jobs render charts on different
worker threads at once. ``bins`` is bounded to 1..MAX_BINS and each series to MAX_POINTS points,
since the spec comes from the model and the model can be steered by data (review M4).

Defaults: a title derived from the axis labels, axis labels from the series names, ``count``
on a histogram's y axis, thousands separators on value axes, date-aware x axes for time series,
rotated or horizontal category labels when they would overlap.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from analyst_agent.tools.base import Tool, ToolContext, ToolResult

CHART_TYPES = ("bar", "barh", "line", "scatter", "hist", "pie")
AUTO = "auto"
MAX_VERTICAL_BARS = 12
LONG_LABEL_CHARS = 14
MAX_BINS = 200
MAX_POINTS = 10_000

# 2024-03-01, 2024-03, 2024/03/01, 2024-03-01T10:00:00, 2024-Q1, 2024Q1, 2024-W05
_DATE_STR_RE = re.compile(
    r"^\d{4}([-/](0[1-9]|1[0-2])([-/]\d{2})?([T ]\d{2}:\d{2}(:\d{2})?.*)?|-?Q[1-4]|-?W\d{2})$"
)
_MONTH_NAMES = (
    "jan feb mar apr may jun jul aug sep oct nov dec january february march april june july "
    "august september october november december"
).split()


def _is_number(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _is_temporal(v: Any) -> bool:
    if isinstance(v, date | datetime):
        return True
    if isinstance(v, str):
        s = v.strip()
        if _DATE_STR_RE.match(s):
            return True
        low = s.lower()
        # "Jan 2025", "March 2024", "2025-Jan"
        return any(
            re.fullmatch(rf"({m})[ -]?\d{{2,4}}|\d{{4}}[ -]?({m})", low) for m in _MONTH_NAMES
        )
    return False


def _is_year(v: Any) -> bool:
    if isinstance(v, str) and v.strip().isdigit():
        v = int(v.strip())
    return _is_number(v) and float(v).is_integer() and 1900 <= v <= 2100


def infer_chart_type(x: Sequence[Any] | None, y: Sequence[Any] | None = None) -> tuple[str, str]:
    """Pick a chart type from the data shape. Returns ``(chart_type, reason)``."""
    if not x:
        return "hist", "only values were given: showing their distribution"
    if all(_is_temporal(v) for v in x):
        return "line", "x values are dates/periods: time series"
    if len(x) >= 2 and all(_is_year(v) for v in x):
        return "line", "x values are years: time series"
    if all(_is_number(v) for v in x):
        increasing = all(b > a for a, b in zip(x, x[1:], strict=False))
        if increasing and len(x) >= 3:
            return "line", "numeric x is strictly increasing: trend"
        return "scatter", "numeric x and y: relationship between two measures"
    labels = [str(v) for v in x]
    if len(labels) > MAX_VERTICAL_BARS or max(len(lab) for lab in labels) > LONG_LABEL_CHARS:
        return "barh", "categorical x with many or long labels: horizontal bar comparison"
    return "bar", "categorical x: comparison across categories"


def _to_datetimes(x: Sequence[Any]) -> list[Any] | None:
    """Parse date-like x values for a proper time axis; None if any value cannot be parsed."""
    import pandas as pd

    if all(isinstance(v, date | datetime) for v in x):
        return list(x)
    if not all(isinstance(v, str) for v in x):
        return None
    try:
        parsed = pd.to_datetime(pd.Series(x), errors="raise", format="mixed")
    except (ValueError, TypeError):
        return None
    return list(parsed.dt.to_pydatetime())


def _default_title(spec: dict[str, Any], kind: str) -> str:
    xl, yl = spec.get("x_label"), spec.get("y_label")
    if kind == "hist":
        return f"Distribution of {yl or xl or 'values'}"
    if yl and xl:
        return f"{yl} by {xl}"
    return yl or "Chart"


def make_chart(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.ticker import FuncFormatter

    if not isinstance(args.get("spec"), dict):
        return ToolResult("`spec` must be an object.", True)
    spec = dict(args["spec"])
    requested = spec.get("chart_type") or AUTO
    if requested != AUTO and requested not in CHART_TYPES:
        return ToolResult(
            f"Unsupported chart_type {requested!r}; use one of {CHART_TYPES} or omit it", True
        )
    series = spec.get("series") or [
        {"name": spec.get("y_label", ""), "x": spec.get("x"), "y": spec.get("y")}
    ]
    if not isinstance(series, list) or not all(isinstance(s, dict) for s in series):
        return ToolResult("`series` must be a list of {name, x, y} objects.", True)

    reason = "requested"
    kind = requested
    if kind == AUTO:
        kind, reason = infer_chart_type(series[0].get("x"), series[0].get("y"))

    for s in series:
        if not s.get("y"):
            return ToolResult(
                "Each series needs non-empty `y` (and `x` unless chart_type is hist/pie).", True
            )
        if s.get("x") is None and kind not in ("hist", "pie"):
            return ToolResult(
                "Each series needs `y` (and `x` unless chart_type is hist/pie).", True
            )
        if s.get("x") is not None and len(s["x"]) != len(s["y"]):
            return ToolResult("Series `x` and `y` must have the same length.", True)
        if not all(_is_number(v) or v is None for v in s["y"]):
            return ToolResult("Series `y` values must be numbers.", True)
        if len(s["y"]) > MAX_POINTS:
            return ToolResult(
                f"Too many points ({len(s['y'])}); aggregate to <= {MAX_POINTS}.", True
            )
    bins = spec.get("bins")
    if bins is not None and (
        isinstance(bins, bool) or not isinstance(bins, int) or not 1 <= bins <= MAX_BINS
    ):
        return ToolResult(f"`bins` must be an integer between 1 and {MAX_BINS}.", True)

    title = spec.get("title") or _default_title(spec, kind)
    x_label = spec.get("x_label") or ""
    y_label = spec.get("y_label") or (series[0].get("name") if len(series) == 1 else "") or ""
    if kind == "hist":
        x_label = x_label or y_label or "value"
        y_label = "count"

    thousands = FuncFormatter(lambda v, _pos: f"{v:,.0f}" if abs(v) >= 1000 else f"{v:g}")
    n_categories = max(len(s.get("x") or []) for s in series)
    height = max(5.0, 0.35 * n_categories) if kind == "barh" else 5.0
    fig = Figure(figsize=(8, min(height, 14.0)), dpi=120)
    FigureCanvasAgg(fig)
    ax = fig.add_subplot()
    bar_width = 0.8 / len(series)
    for idx, s in enumerate(series):
        label = s.get("name") or None
        ys = [float("nan") if v is None else v for v in s["y"]]
        xs = s.get("x")
        if kind in ("bar", "barh"):
            cats = [str(v) for v in xs]
            pos = [i + (idx - (len(series) - 1) / 2) * bar_width for i in range(len(cats))]
            if kind == "bar":
                ax.bar(pos, ys, width=bar_width, label=label)
                ax.set_xticks(range(len(cats)), cats)
            else:
                ax.barh(pos, ys, height=bar_width, label=label)
                ax.set_yticks(range(len(cats)), cats)
        elif kind == "line":
            dts = _to_datetimes(xs) if all(_is_temporal(v) for v in xs) else None
            ax.plot(dts if dts is not None else xs, ys, marker="o", markersize=3, label=label)
        elif kind == "scatter":
            ax.scatter(xs, ys, label=label, alpha=0.8)
        elif kind == "hist":
            values = [v for v in ys if v == v]  # drop NaN
            ax.hist(values, bins=bins or "auto", label=label, alpha=0.85)
        elif kind == "pie":
            ax.pie(ys, labels=[str(v) for v in (xs or [])] or None, autopct="%1.1f%%")

    ax.set_title(title)
    if kind == "barh":
        ax.invert_yaxis()  # first category on top, as in the data
        ax.set_xlabel(y_label)
        ax.set_ylabel(x_label)
        ax.xaxis.set_major_formatter(thousands)
    elif kind != "pie":
        ax.set_xlabel(x_label)
        ax.set_ylabel(y_label)
        ax.yaxis.set_major_formatter(thousands)
        ax.grid(axis="y", alpha=0.3)
    if kind == "bar" and (
        n_categories > 6 or max(len(str(v)) for v in series[0]["x"]) > LONG_LABEL_CHARS // 2
    ):
        for tick in ax.get_xticklabels():
            tick.set_rotation(45)
            tick.set_horizontalalignment("right")
    if kind == "line":
        fig.autofmt_xdate()
    if len(series) > 1:
        ax.legend()
    fig.tight_layout()

    ctx.artifacts_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "chart"
    digest = hashlib.sha1(json.dumps(spec, sort_keys=True, default=str).encode()).hexdigest()
    path = ctx.artifacts_dir / f"{slug}-{digest[:8]}.png"
    fig.savefig(path)
    note = f" (chart_type={kind}: {reason})" if requested == AUTO else ""
    return ToolResult(
        f"Chart saved to {path}{note}",
        data={"path": str(path), "chart_type": kind, "chart_type_reason": reason, "title": title},
    )


_SERIES_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "x": {"type": "array", "items": {}},
        "y": {"type": "array", "items": {"type": ["number", "null"]}},
    },
    "required": ["y"],
}

MAKE_CHART = Tool(
    name="make_chart",
    description=(
        "Render a matplotlib chart from data you already computed and save it as a PNG under "
        "artifacts/. Omit chart_type (or pass 'auto') to pick it from the data: dates/periods "
        "on x -> line, categories -> bar, only y values -> histogram. Returns the file path; "
        "reference it in the report as ![title](path)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "spec": {
                "type": "object",
                "properties": {
                    "chart_type": {"type": "string", "enum": [AUTO, *CHART_TYPES]},
                    "title": {"type": "string"},
                    "x_label": {"type": "string"},
                    "y_label": {"type": "string"},
                    "x": {"type": "array", "items": {}, "description": "Single-series x values."},
                    "y": {"type": "array", "items": {"type": ["number", "null"]}},
                    "series": {"type": "array", "items": _SERIES_SCHEMA},
                    "bins": {"type": "integer", "minimum": 1, "maximum": MAX_BINS},
                },
            }
        },
        "required": ["spec"],
        "additionalProperties": False,
    },
    handler=make_chart,
)
