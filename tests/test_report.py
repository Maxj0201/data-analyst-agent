"""Structured report schema, rendering and the headline / key-number consistency check."""

from __future__ import annotations

from typing import Any

import pytest

from analyst_agent.core.critic import build_critic_input
from analyst_agent.core.report import (
    STRUCTURED_FINAL_REPORT_INPUT_SCHEMA,
    ReportValidationError,
    StructuredReport,
    check_consistency,
    extract_numbers,
    missing_chart_paths,
    parse_structured_report,
    structured_final_report_schema,
)
from analyst_agent.tools import build_default_registry

VALID: dict[str, Any] = {
    "headline": "South leads with revenue of 200, ahead of north at 150.",
    "key_numbers": {"south_revenue": 200, "north_revenue": 150, "top_region": "south"},
    "method": "Summed revenue by region over the sales table.",
    "caveats": ["One south row has NULL revenue and is excluded."],
    "charts": [{"path": "artifacts/rev.png", "caption": "Revenue by region"}],
}


def sql_record(output: str, rows: list[dict[str, Any]] | None = None, **kw: Any) -> dict[str, Any]:
    return {
        "name": "run_sql",
        "input": {"query": "SELECT 1"},
        "output": output,
        "is_error": False,
        "data": {"rows": rows or []},
        **kw,
    }


# --------------------------------------------------------------------------- schema / parse


def test_schema_requires_every_section() -> None:
    assert set(STRUCTURED_FINAL_REPORT_INPUT_SCHEMA["required"]) == {
        "headline",
        "key_numbers",
        "method",
        "caveats",
        "charts",
    }
    assert structured_final_report_schema()["name"] == "final_report"


def test_parse_valid_report_and_render_markdown() -> None:
    rep = parse_structured_report(VALID)
    md = rep.to_markdown()
    sections = ["## Answer", "## Key numbers", "## Method", "## Caveats", "## Charts"]
    positions = [md.index(s) for s in sections]
    assert positions == sorted(positions), "sections must render in a fixed order"
    assert "| south_revenue | 200 |" in md and "| top_region | south |" in md
    assert "![Revenue by region](artifacts/rev.png)" in md
    assert StructuredReport.from_dict(rep.to_dict()) == rep


def test_parse_collects_every_error() -> None:
    with pytest.raises(ReportValidationError) as exc:
        parse_structured_report(
            {"headline": " ", "key_numbers": {}, "method": "", "caveats": "x", "charts": [{}]}
        )
    joined = " ".join(exc.value.errors)
    for field in ("headline", "key_numbers", "method", "caveats", "charts"):
        assert f"`{field}`" in joined


def test_empty_caveats_and_charts_render_cleanly() -> None:
    rep = parse_structured_report({**VALID, "caveats": [], "charts": []})
    md = rep.to_markdown()
    assert "- None noted." in md and "## Charts" not in md


def test_large_numbers_render_with_separators_and_pipes_escaped() -> None:
    rep = parse_structured_report({**VALID, "key_numbers": {"a|b": 1234567.5}})
    assert "| a\\|b | 1,234,567.5 |" in rep.to_markdown()


def test_missing_chart_paths() -> None:
    rep = parse_structured_report(VALID)
    assert missing_chart_paths(rep, ["artifacts/rev.png"]) == []
    assert missing_chart_paths(rep, ["./artifacts/rev.png"]) == []
    assert missing_chart_paths(rep, []) == ["artifacts/rev.png"]


def test_final_report_handler_accepts_structured_and_legacy(ctx) -> None:
    reg = build_default_registry()
    res = reg.execute("final_report", VALID, ctx)
    assert res.terminate and not res.is_error
    assert res.data["markdown"].startswith("## Answer")
    assert res.data["structured"]["headline"] == VALID["headline"]
    assert res.data["key_numbers"] == VALID["key_numbers"]

    bad = reg.execute("final_report", {**VALID, "method": ""}, ctx)
    assert bad.is_error and "`method`" in bad.content

    legacy = reg.execute("final_report", {"markdown": "# R", "key_numbers": {"a": 1}}, ctx)
    assert legacy.data == {"markdown": "# R", "key_numbers": {"a": 1}}
    assert reg.execute("final_report", {"key_numbers": {}}, ctx).is_error


# --------------------------------------------------------------------------- number extraction


@pytest.mark.parametrize(
    ("text", "value", "percent"),
    [
        ("$1.2M", 1_200_000, False),
        ("1,234", 1234, False),
        ("12.5%", 12.5, True),
        ("3.4 million", 3_400_000, False),
        ("€45k", 45_000, False),
        ("0.137", 0.137, False),
    ],
)
def test_extract_numbers_formats(text: str, value: float, percent: bool) -> None:
    (tok,) = extract_numbers(f"value: {text} here")
    assert tok.value == pytest.approx(value) and tok.percent is percent


def test_extract_numbers_skips_dates_ordinals_and_identifiers() -> None:
    toks = extract_numbers("On 2025-03-01, the 5th order in Q1 for L2 sku_9 was v2")
    assert toks == []
    (year,) = extract_numbers("in 2024")
    assert year.date_like


# --------------------------------------------------------------------------- consistency


def test_consistent_report_passes() -> None:
    rep = parse_structured_report(VALID)
    records = [sql_record("| region | rev |\n| north | 150 |\n| south | 200 |")]
    check = check_consistency(rep, records, question="Which region has the most revenue?")
    assert check.ok, check.issues
    assert check.headline_numbers == 2 and check.key_numbers_checked == 3
    assert check.as_critic_notes() == ""


def test_headline_number_missing_from_key_numbers_is_flagged() -> None:
    rep = parse_structured_report({**VALID, "headline": "South leads with 200, 33% more."})
    check = check_consistency(rep, [sql_record("north 150 south 200")])
    assert [(i.kind, i.text) for i in check.issues] == [("not_in_key_numbers", "33%")]
    assert "33%" in check.as_critic_notes()


def test_untraced_key_number_is_flagged() -> None:
    rep = parse_structured_report({**VALID, "key_numbers": {**VALID["key_numbers"], "total": 350}})
    check = check_consistency(rep, [sql_record("north 150 south 200")])
    assert [(i.kind, i.name) for i in check.issues] == [("untraced", "total")]


def test_untraced_categorical_key_number_is_flagged() -> None:
    rep = parse_structured_report({**VALID, "key_numbers": {"top_region": "west"}})
    rep.headline = "West leads."
    check = check_consistency(rep, [sql_record("north 150 south 200")])
    assert [(i.kind, i.name) for i in check.issues] == [("untraced", "top_region")]


def test_errored_and_non_evidence_records_do_not_count() -> None:
    rep = parse_structured_report(VALID)
    records = [
        sql_record("north 150 south 200", is_error=True),
        {"name": "make_chart", "output": "north 150 south 200", "is_error": False, "data": {}},
    ]
    check = check_consistency(rep, records)
    assert {i.name for i in check.issues} == {"south_revenue", "north_revenue", "top_region"}


def test_rounding_scaling_and_percent_forms_match() -> None:
    rep = parse_structured_report(
        {
            **VALID,
            "headline": "Revenue was $1.2M and churn was 13.7% across 2025.",
            "key_numbers": {"revenue": 1_234_567.89, "churn_rate": 0.1372},
        }
    )
    # Raw cells (full precision) from query rows; the preview shows rounded values.
    records = [
        sql_record(
            "| revenue | churn_rate |\n| 1234567.89 | 0.14 |",
            rows=[{"revenue": 1234567.89, "churn_rate": 0.13724}],
        )
    ]
    check = check_consistency(rep, records, question="Revenue and churn in 2025?")
    assert check.ok, check.issues


def test_numbers_from_the_question_are_exempt() -> None:
    rep = parse_structured_report(
        {**VALID, "headline": "The top 2 regions are south (200) and north (150)."}
    )
    check = check_consistency(rep, [sql_record("north 150 south 200")], question="Top 2 regions?")
    assert check.ok, check.issues


def test_python_stdout_counts_as_evidence() -> None:
    rep = parse_structured_report(
        {**VALID, "headline": "Median is 42.5.", "key_numbers": {"median": 42.5}}
    )
    rec = {
        "name": "run_python",
        "input": {"code": "print(x)"},
        "output": "stdout:\n42.5",
        "is_error": False,
        "data": {"stdout": "42.5\n"},
    }
    assert check_consistency(rep, [rec]).ok


def test_critic_input_carries_consistency_notes() -> None:
    notes = "flagged: total"
    content = build_critic_input("q", "draft", {"total": 1}, [], consistency_notes=notes)
    assert "## Automated consistency check\nflagged: total" in content
    assert "Automated consistency check" not in build_critic_input("q", "d", {}, [])
