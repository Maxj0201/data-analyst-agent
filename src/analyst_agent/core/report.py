"""Structured final reports and the headline / key-number consistency check.

With ``AGENT_STRUCTURED_REPORT_ENABLED`` on, the agent must submit ``final_report`` with a fixed
schema (headline answer, key_numbers table, method, caveats, charts) instead of free Markdown.
The loop renders the Markdown from those fields, so every report has the same shape, and runs
:func:`check_consistency` before the critic:

* every number in the headline must appear among the ``key_numbers`` values, and
* every ``key_numbers`` value must trace back to the output of an executed tool call
  (``run_sql`` / ``run_python`` / ``inspect_schema``).

The check is deterministic and deliberately forgiving about formatting (``1.2M``, ``12.5%``,
``$1,234``, rounding to the displayed precision). It never blocks a report by itself: unsupported
numbers are passed to the critic as flagged items, and the critic decides.

This module has no dependency on ``tools`` or the loop, so the ``final_report`` handler can use
it too.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# Tools whose output counts as evidence for a number.
EVIDENCE_TOOLS = ("run_sql", "run_python", "inspect_schema")
MAX_HEADLINE_CHARS = 600

# --------------------------------------------------------------------------- schema

_KEY_NUMBERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        'Metric name -> value, e.g. {"total_revenue": 12345.6, "top_region": "south"}. '
        "Every number in the headline must be one of these values, and every value must come "
        "from a tool result."
    ),
    "additionalProperties": {"type": ["number", "string", "null"]},
}

_CHART_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Path returned by make_chart / run_python."},
        "caption": {"type": "string"},
    },
    "required": ["path"],
    "additionalProperties": False,
}

STRUCTURED_FINAL_REPORT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {
            "type": "string",
            "description": "One or two sentences that directly answer the question.",
        },
        "key_numbers": _KEY_NUMBERS_SCHEMA,
        "method": {
            "type": "string",
            "description": "How the answer was computed: tables, filters, definitions, queries.",
        },
        "caveats": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Assumptions and data-quality caveats; [] if there are none.",
        },
        "charts": {
            "type": "array",
            "items": _CHART_SCHEMA,
            "description": "Charts created in this run to include; [] if none.",
        },
        "details": {
            "type": "string",
            "description": "Optional supporting Markdown (tables, breakdowns).",
        },
    },
    "required": ["headline", "key_numbers", "method", "caveats", "charts"],
    "additionalProperties": False,
}


def structured_final_report_schema() -> dict[str, Any]:
    """Tool definition sent to the model for ``final_report`` when structured reports are on."""
    return {
        "name": "final_report",
        "description": (
            "Submit the final report as structured fields: a headline answer, the key numbers, "
            "the method, caveats and charts. Call once when the analysis is complete and "
            "verified; this ends the analysis (a reviewer may send it back with feedback). "
            "Every number in the headline must appear in key_numbers, and every key number "
            "must come from a tool result."
        ),
        "input_schema": STRUCTURED_FINAL_REPORT_INPUT_SCHEMA,
    }


# --------------------------------------------------------------------------- report model


class ReportValidationError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class ChartRef:
    path: str
    caption: str = ""


@dataclass
class StructuredReport:
    headline: str
    key_numbers: dict[str, Any]
    method: str
    caveats: list[str] = field(default_factory=list)
    charts: list[ChartRef] = field(default_factory=list)
    details: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> StructuredReport:
        return cls(
            headline=d["headline"],
            key_numbers=dict(d["key_numbers"]),
            method=d["method"],
            caveats=list(d.get("caveats") or []),
            charts=[ChartRef(**c) for c in d.get("charts") or []],
            details=d.get("details") or "",
        )

    def to_markdown(self) -> str:
        parts = ["## Answer", self.headline.strip(), "", "## Key numbers", "| Metric | Value |"]
        parts.append("|---|---|")
        for name, value in self.key_numbers.items():
            parts.append(f"| {_cell(name)} | {_cell(format_value(value))} |")
        if self.details.strip():
            parts += ["", self.details.strip()]
        parts += ["", "## Method", self.method.strip(), "", "## Caveats"]
        parts += [f"- {c.strip()}" for c in self.caveats] or ["- None noted."]
        if self.charts:
            parts += ["", "## Charts"]
            for c in self.charts:
                caption = c.caption.strip() or "chart"
                parts.append(f"![{caption}]({c.path})")
        return "\n".join(parts) + "\n"


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def format_value(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int | float):
        return format(value, ",")
    return str(value)


def is_structured(args: Mapping[str, Any]) -> bool:
    """True when a ``final_report`` call uses the structured fields."""
    return any(k in args for k in ("headline", "method", "caveats", "charts"))


def parse_structured_report(args: Mapping[str, Any]) -> StructuredReport:
    """Validate ``final_report`` arguments against the structured schema.

    Raises :class:`ReportValidationError` listing every problem, so the agent can fix them all
    in one retry.
    """
    errors: list[str] = []

    headline = args.get("headline")
    if not isinstance(headline, str) or not headline.strip():
        errors.append("`headline` must be a non-empty string that directly answers the question.")
    elif len(headline) > MAX_HEADLINE_CHARS:
        errors.append(f"`headline` must be at most {MAX_HEADLINE_CHARS} characters.")

    key_numbers = args.get("key_numbers")
    if not isinstance(key_numbers, dict) or not key_numbers:
        errors.append("`key_numbers` must be a non-empty object of metric name -> value.")
        key_numbers = {}
    else:
        for name, value in key_numbers.items():
            if not str(name).strip():
                errors.append("`key_numbers` names must be non-empty.")
            if value is not None and not isinstance(value, int | float | str):
                errors.append(f"`key_numbers[{name!r}]` must be a number, string or null.")
            elif isinstance(value, float) and not math.isfinite(value):
                errors.append(f"`key_numbers[{name!r}]` must be finite.")

    method = args.get("method")
    if not isinstance(method, str) or not method.strip():
        errors.append("`method` must be a non-empty string describing how the answer was computed.")

    caveats = args.get("caveats")
    if not isinstance(caveats, list) or not all(isinstance(c, str) for c in caveats):
        errors.append("`caveats` must be a list of strings ([] if there are none).")
        caveats = []

    charts_raw = args.get("charts")
    charts: list[ChartRef] = []
    if not isinstance(charts_raw, list):
        errors.append("`charts` must be a list of {path, caption} objects ([] if none).")
    else:
        for c in charts_raw:
            if isinstance(c, str) and c.strip():
                charts.append(ChartRef(path=c.strip()))
            elif isinstance(c, dict) and isinstance(c.get("path"), str) and c["path"].strip():
                charts.append(ChartRef(path=c["path"].strip(), caption=str(c.get("caption") or "")))
            else:
                errors.append("each `charts` entry needs a non-empty `path`.")

    details = args.get("details", args.get("markdown", "")) or ""
    if not isinstance(details, str):
        errors.append("`details` must be a string.")
        details = ""

    if errors:
        raise ReportValidationError(errors)
    assert isinstance(headline, str) and isinstance(method, str)
    return StructuredReport(
        headline=headline.strip(),
        key_numbers=dict(key_numbers),
        method=method.strip(),
        caveats=[c for c in caveats if c.strip()],
        charts=charts,
        details=details,
    )


def missing_chart_paths(report: StructuredReport, artifacts: Iterable[str]) -> list[str]:
    """Chart paths the report cites that were not produced by a tool in this run."""
    produced = {_norm_path(a) for a in artifacts}
    return [c.path for c in report.charts if _norm_path(c.path) not in produced]


def _norm_path(p: str) -> str:
    return p.strip().removeprefix("./").replace("\\", "/")


# --------------------------------------------------------------------------- number extraction

# ISO dates / year-months are dates, not quantities.
_DATE_RE = re.compile(r"\b\d{4}-\d{2}(?:-\d{2})?(?:[T ]\d{2}:\d{2}(?::\d{2})?)?\b")
_NUM_RE = re.compile(
    r"""
    (?<![\w.])                                  # not inside a word or after a decimal point
    (?P<cur>[$€£¥])?
    (?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)
    (?P<suf>\s?(?:%|percent\b|pp\b|bn\b|[kKmMbB]\b|thousand\b|million\b|billion\b))?
    (?![\w])                                    # not "5th", "3D", "v2a"
    """,
    re.X,
)
_SCALES = {
    "k": 1e3,
    "thousand": 1e3,
    "m": 1e6,
    "million": 1e6,
    "b": 1e9,
    "bn": 1e9,
    "billion": 1e9,
}


@dataclass(frozen=True)
class NumberToken:
    """A number as written in text, with the tolerance implied by its displayed precision."""

    text: str
    value: float
    tolerance: float
    percent: bool = False
    date_like: bool = False

    def candidates(self) -> list[tuple[float, float]]:
        """``(value, tolerance)`` readings: ``12.5%`` may be stored as 12.5 or 0.125."""
        out = [(self.value, self.tolerance)]
        if self.percent:
            out.append((self.value / 100, self.tolerance / 100))
        return out


def extract_numbers(text: str) -> list[NumberToken]:
    """All numbers written in ``text`` (dates removed). Signs are ignored."""
    text = _DATE_RE.sub(" ", text)
    out: list[NumberToken] = []
    for m in _NUM_RE.finditer(text):
        raw = m.group("num")
        suf = (m.group("suf") or "").strip().lower()
        decimals = len(raw.split(".", 1)[1]) if "." in raw else 0
        scale = _SCALES.get(suf, 1.0)
        value = float(raw.replace(",", "")) * scale
        tol = 0.5 * 10**-decimals * scale
        percent = suf in ("%", "percent", "pp")
        date_like = (
            not suf
            and not m.group("cur")
            and "," not in raw
            and "." not in raw
            and 1900 <= value <= 2100
        )
        out.append(NumberToken(m.group(0).strip(), value, tol, percent, date_like))
    return out


def _value_token(value: float) -> NumberToken:
    """Token for an exact value (e.g. a key number or a raw query cell)."""
    if isinstance(value, int) or float(value).is_integer():
        return NumberToken(str(value), float(value), 0.5 if isinstance(value, int) else 1e-9)
    r = repr(float(value))
    decimals = len(r.split(".", 1)[1]) if "." in r and "e" not in r else 12
    return NumberToken(r, float(value), 0.5 * 10**-decimals)


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        toks = extract_numbers(value)
        if len(toks) == 1 and toks[0].text.replace(" ", "") == value.strip().replace(" ", ""):
            return toks[0].value
    return None


def _close(a: float, tol_a: float, b: float, tol_b: float) -> bool:
    a, b = abs(a), abs(b)
    return abs(a - b) <= max(tol_a, tol_b) + 1e-9 * max(1.0, abs(b))


def _token_matches(tok: NumberToken, target: NumberToken) -> bool:
    return any(_close(v, t, tv, tt) for v, t in tok.candidates() for tv, tt in target.candidates())


# --------------------------------------------------------------------------- consistency check

IssueKind = Literal["not_in_key_numbers", "untraced"]


@dataclass(frozen=True)
class NumberIssue:
    kind: IssueKind
    text: str
    name: str | None = None  # key_numbers entry, for "untraced"

    def describe(self) -> str:
        if self.kind == "not_in_key_numbers":
            return f'Headline number "{self.text}" does not match any key_numbers value.'
        return (
            f"key_numbers[{self.name!r}] = {self.text} was not found in any successful tool "
            "result (computed or typed outside the tools?)."
        )


@dataclass
class ConsistencyReport:
    issues: list[NumberIssue] = field(default_factory=list)
    headline_numbers: int = 0
    key_numbers_checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "headline_numbers": self.headline_numbers,
            "key_numbers_checked": self.key_numbers_checked,
            "issues": [asdict(i) for i in self.issues],
        }

    def as_critic_notes(self) -> str:
        """Text for the critic; "" when there is nothing to flag."""
        if self.ok:
            return ""
        lines = [
            "An automated consistency check (deterministic; it may have false positives, e.g. "
            "for values derived by simple arithmetic) flagged these numbers as unsupported:"
        ]
        lines += [f"- {i.describe()}" for i in self.issues]
        lines.append(
            "Check each one against the executed steps. A number that no executed result "
            "supports is a reason to revise."
        )
        return "\n".join(lines)


def _evidence(tool_records: Iterable[Mapping[str, Any]]) -> tuple[list[NumberToken], str]:
    """Numbers and lower-cased text from successful evidence-tool results."""
    tokens: list[NumberToken] = []
    texts: list[str] = []
    for rec in tool_records:
        if rec.get("name") not in EVIDENCE_TOOLS or rec.get("is_error"):
            continue
        out = str(rec.get("output") or "")
        data = rec.get("data") or {}
        texts.append(out)
        tokens.extend(extract_numbers(out))
        stdout = data.get("stdout")
        if isinstance(stdout, str) and stdout not in out:
            texts.append(stdout)
            tokens.extend(extract_numbers(stdout))
        for row in data.get("rows") or []:
            cells = row.values() if isinstance(row, Mapping) else row
            for cell in cells:
                if isinstance(cell, bool) or cell is None:
                    continue
                if isinstance(cell, int | float) and math.isfinite(cell):
                    tokens.append(_value_token(cell))
                else:
                    texts.append(str(cell))
    return tokens, "\n".join(texts).lower()


def check_consistency(
    report: StructuredReport,
    tool_records: Iterable[Mapping[str, Any]],
    question: str = "",
) -> ConsistencyReport:
    """Check headline numbers against key_numbers, and key_numbers against tool results.

    ``tool_records`` are ``asdict(ToolCallRecord)``-shaped dicts. Numbers that appear in the
    question (``top 5``, ``in 2024``) and bare years in the headline are not required to be
    key numbers.
    """
    result = ConsistencyReport()
    question_tokens = extract_numbers(question)

    key_tokens: dict[str, NumberToken] = {}
    for name, value in report.key_numbers.items():
        num = _as_number(value)
        if num is not None:
            tok = (
                extract_numbers(value)[0] if isinstance(value, str) else _value_token(value)  # type: ignore[arg-type]
            )
            key_tokens[name] = tok

    for tok in extract_numbers(report.headline):
        if tok.date_like or any(_token_matches(tok, q) for q in question_tokens):
            continue
        result.headline_numbers += 1
        if not any(_token_matches(tok, k) for k in key_tokens.values()):
            result.issues.append(NumberIssue("not_in_key_numbers", tok.text))

    evidence, evidence_text = _evidence(tool_records)
    for name, value in report.key_numbers.items():
        if value is None or value == "":
            continue
        result.key_numbers_checked += 1
        if name in key_tokens:
            tok = key_tokens[name]
            # A key number may be stored as a fraction and shown as a percentage, or vice versa.
            readings = [tok, NumberToken(tok.text, tok.value * 100, tok.tolerance * 100)]
            readings.append(NumberToken(tok.text, tok.value / 100, tok.tolerance / 100))
            if not any(_token_matches(r, e) for r in readings for e in evidence):
                result.issues.append(NumberIssue("untraced", format_value(value), name))
        elif str(value).strip().lower() not in evidence_text:
            result.issues.append(NumberIssue("untraced", repr(value), name))
    return result
