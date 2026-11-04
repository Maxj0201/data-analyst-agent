"""One settings class; every field documented in docs/configuration.md and .env.example."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from analyst_agent.core.config import Settings

REPO = Path(__file__).resolve().parents[1]
DOC = (REPO / "docs/configuration.md").read_text()
ENV_EXAMPLE = (REPO / ".env.example").read_text()


def _default(name: str) -> str:
    f = Settings.model_fields[name]
    v = f.default_factory() if f.default_factory else f.default  # type: ignore[call-arg]
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, list):
        return "[]"
    return str(v)


def _doc_rows() -> dict[str, str]:
    """``AGENT_X -> default`` from the "All settings" table."""
    section = DOC.split("## All settings", 1)[1].split("\n## ", 1)[0]
    rows = re.findall(r"^\| `(AGENT_[A-Z0-9_]+)` \| ([^|]*) \|", section, re.M)
    return {k: v.strip().strip("`").replace("*(unset)*", "") for k, v in rows}


def _env_example() -> dict[str, str]:
    return dict(re.findall(r"^#? ?(AGENT_[A-Z0-9_]+)=(.*)$", ENV_EXAMPLE, re.M))


FIELDS = [f"AGENT_{n.upper()}" for n in Settings.model_fields]


def test_every_setting_is_in_the_docs_table_with_its_default() -> None:
    rows = _doc_rows()
    assert sorted(rows) == sorted(FIELDS), "docs/configuration.md table != Settings fields"
    for name in Settings.model_fields:
        assert rows[f"AGENT_{name.upper()}"] == _default(name), name


def test_every_setting_is_in_env_example_with_its_default() -> None:
    env = _env_example()
    settings_vars = {
        k: v for k, v in env.items() if k not in {"AGENT_API_URL", "AGENT_API_ID_TOKEN"}
    }
    assert sorted(settings_vars) == sorted(FIELDS)
    for name in Settings.model_fields:
        assert settings_vars[f"AGENT_{name.upper()}"] == _default(name), name


def test_feature_flags_default_off() -> None:
    s = Settings(_env_file=None)
    assert not (
        s.routing_enabled
        or s.semantic_layer_enabled
        or s.approval_enabled
        or s.structured_report_enabled
        or s.memory_enabled
    )


def test_no_other_settings_classes() -> None:
    src = REPO / "src/analyst_agent"
    offenders = [
        str(p.relative_to(REPO))
        for p in src.rglob("*.py")
        if "BaseSettings" in p.read_text() and p.name != "config.py"
    ]
    assert offenders == []


@pytest.mark.parametrize(
    ("var", "attr"),
    [
        ("AGENT_JOB_DB_PATH", "job_db_path"),
        ("AGENT_APPROVAL_ROW_THRESHOLD", "approval_row_threshold"),
        ("AGENT_MODEL_L1", "model_l1"),
    ],
)
def test_empty_value_means_unset(monkeypatch: pytest.MonkeyPatch, var: str, attr: str) -> None:
    monkeypatch.setenv(var, "")
    assert getattr(Settings(_env_file=None), attr) is None
