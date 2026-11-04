"""Schema for ``metrics.yaml`` files: governed metric definitions for the semantic layer.

A metrics file looks like::

    version: 1
    domain: saas                      # default domain for metrics that omit it
    metrics:
      - name: churn_rate
        label: Churn rate              # optional human-readable name
        domain: saas                   # optional if the file sets a default
        definition: Share of subscriptions active at the start of a period that churn in it.
        sql_fragment: |
          SELECT ... FROM subscriptions ...
        tables: [subscriptions]        # tables the fragment reads; used to filter retrieval
        synonyms: [customer churn, logo churn, cancellation rate]
        caveats:
          - Cohort-based: new subscriptions started during the period are excluded.

``sql_fragment`` must be a read-only ``SELECT`` / ``WITH`` statement in DuckDB dialect.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_READ_ONLY_RE = re.compile(r"^\s*(?:--[^\n]*\n\s*)*(select|with)\b", re.IGNORECASE)


class Metric(BaseModel):
    """One governed metric definition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(description="snake_case identifier, unique across all loaded files")
    label: str | None = None
    domain: str
    definition: str = Field(min_length=1)
    sql_fragment: str = Field(min_length=1)
    tables: list[str] = Field(default_factory=list)
    synonyms: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _snake_case(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(f"metric name must be snake_case ([a-z][a-z0-9_]*), got {v!r}")
        return v

    @field_validator("domain", "definition")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v.strip()

    @field_validator("sql_fragment")
    @classmethod
    def _read_only(cls, v: str) -> str:
        v = v.strip()
        if not _READ_ONLY_RE.match(v):
            raise ValueError("sql_fragment must be a read-only SELECT or WITH statement")
        if ";" in v.rstrip(";"):
            raise ValueError("sql_fragment must be a single statement")
        return v.rstrip(";").rstrip()

    @field_validator("synonyms", "caveats", "tables")
    @classmethod
    def _clean_list(cls, v: list[str]) -> list[str]:
        out = [s.strip() for s in v]
        if any(not s for s in out):
            raise ValueError("list entries must not be blank")
        return out

    @property
    def display_name(self) -> str:
        return self.label or self.name.replace("_", " ")

    def search_text(self, name_boost: int = 2) -> str:
        """Text indexed by the retriever: name + label + synonyms + definition.

        Name, label and synonyms are repeated ``name_boost`` times so that a question naming a
        metric outranks one that merely shares formula words (e.g. "unit_price") with it.
        """
        names = [self.name.replace("_", " "), self.label or "", *self.synonyms]
        parts = names * max(1, name_boost) + [self.definition]
        return " ".join(p for p in parts if p)


class MetricsFile(BaseModel):
    """Top-level structure of one ``metrics.yaml`` file."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    domain: str | None = None
    description: str | None = None
    metrics: list[Metric] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _apply_default_domain(cls, data: object) -> object:
        if isinstance(data, dict) and data.get("domain"):
            metrics = data.get("metrics")
            if isinstance(metrics, list):
                data = dict(data)
                data["metrics"] = [
                    {"domain": data["domain"], **m} if isinstance(m, dict) else m for m in metrics
                ]
        return data

    @field_validator("version")
    @classmethod
    def _supported_version(cls, v: int) -> int:
        if v != 1:
            raise ValueError(f"unsupported metrics file version {v} (expected 1)")
        return v

    @model_validator(mode="after")
    def _unique_names(self) -> MetricsFile:
        seen: set[str] = set()
        for m in self.metrics:
            if m.name in seen:
                raise ValueError(f"duplicate metric name {m.name!r}")
            seen.add(m.name)
        return self
