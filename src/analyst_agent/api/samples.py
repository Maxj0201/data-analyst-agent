"""Bundled sample datasets (``benchmarks/data``) so a viewer can try the agent without uploading.

Samples are read-only and never copied: each file gets a stable ``ds_<16 hex>`` id derived from
its path relative to the samples directory, and ``POST /ask`` resolves that id the same way it
resolves an upload (see ``create_app``). Only enabled with ``AGENT_SAMPLES_ENABLED``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, Field

from .datasets import _KINDS, _VALIDATORS
from .models import DatasetInfo


class SampleDataset(BaseModel):
    dataset_id: str
    name: str
    """Path relative to the samples directory without the suffix, e.g. ``saas/subscriptions``."""
    title: str
    description: str = ""
    filename: str
    size_bytes: int
    rows: int | None = None
    """Data rows (CSV only; ``None`` when unknown)."""
    columns: list[str]
    questions: list[str] = Field(default_factory=list)
    """Example questions that a single-table run can answer."""


@dataclass(frozen=True)
class _Curated:
    title: str
    description: str
    questions: tuple[str, ...]


# Example questions are single-table on purpose: ``POST /ask`` runs over one file.
_CURATED: dict[str, _Curated] = {
    "saas/subscriptions": _Curated(
        "SaaS subscriptions",
        "1.5k B2B subscriptions with plan, seats, MRR, region and churn.",
        (
            "Which region has the highest MRR?",
            "What is total MRR by plan?",
            "Which churn reason is the most common?",
        ),
    ),
    "ecommerce/orders": _Curated(
        "E-commerce orders",
        "4k orders with quantity, unit price, discount, status and sales channel.",
        (
            "Which sales channel sold the most quantity?",
            "What is total revenue (quantity x unit price) by order status?",
            "How many orders were placed each month?",
        ),
    ),
    "ecommerce/customers": _Curated(
        "E-commerce customers",
        "600 customers with country, segment and signup channel.",
        (
            "Which country has the most customers?",
            "How many customers signed up through each signup channel?",
        ),
    ),
    "ecommerce/products": _Curated(
        "E-commerce products",
        "80 products with category, list price and unit cost.",
        (
            "Which category has the highest average list price?",
            "Which products have the highest margin (list price minus unit cost)?",
        ),
    ),
    "hr/employees": _Curated(
        "HR employees",
        "450 employees with department, level, location, salary and rating.",
        (
            "Which department has the highest total base salary?",
            "What is the attrition rate by department?",
            "How does average base salary vary by job level?",
        ),
    ),
    "hr/payroll": _Curated(
        "HR payroll",
        "4.5k payroll lines: base pay, overtime, bonus, gross and net pay.",
        (
            "Which pay period had the highest total gross pay?",
            "What share of gross pay is overtime pay?",
        ),
    ),
    "support/support_tickets": _Curated(
        "Support tickets",
        "400 tickets with category, priority, channel, resolution time and CSAT.",
        (
            "Which category has the longest average resolution hours?",
            "What is the average CSAT score by channel?",
            "How many tickets are there by priority?",
        ),
    ),
}

_MAX_DEPTH = 3


def sample_id(name: str) -> str:
    return "ds_" + hashlib.sha256(f"sample:{name}".encode()).hexdigest()[:16]


def _count_csv_rows(path: Path) -> int:
    with path.open("rb") as fh:
        lines = sum(1 for line in fh if line.strip())
    return max(lines - 1, 0)


class SampleCatalog:
    """Lists the sample files under ``root``. Built once; the directory is treated as static."""

    def __init__(self, root: Path | None) -> None:
        self.root = root
        self._by_id: dict[str, tuple[SampleDataset, Path]] = {}
        if root is not None and root.is_dir():
            self._scan(root)

    def _scan(self, root: Path) -> None:
        files = sorted(
            p
            for p in root.rglob("*")
            if p.is_file()
            and p.suffix.lower() in _KINDS
            and len(p.relative_to(root).parts) <= _MAX_DEPTH
            and not any(part.startswith(".") for part in p.relative_to(root).parts)
        )
        for path in files:
            name = path.relative_to(root).with_suffix("").as_posix()
            kind = _KINDS[path.suffix.lower()]
            try:
                columns = _VALIDATORS[kind](path)
            except HTTPException:
                continue  # not a readable dataset; leave it out of the list
            curated = _CURATED.get(name)
            sample = SampleDataset(
                dataset_id=sample_id(name),
                name=name,
                title=curated.title if curated else name.replace("/", " / "),
                description=curated.description if curated else "",
                filename=path.name,
                size_bytes=path.stat().st_size,
                rows=_count_csv_rows(path) if kind == "csv" else None,
                columns=columns,
                questions=list(curated.questions) if curated else [],
            )
            self._by_id[sample.dataset_id] = (sample, path)
        # Curated samples first (in the order above), then everything else alphabetically.
        order = {name: i for i, name in enumerate(_CURATED)}
        self._by_id = dict(
            sorted(
                self._by_id.items(),
                key=lambda kv: (order.get(kv[1][0].name, len(order)), kv[1][0].name),
            )
        )

    def list(self) -> list[SampleDataset]:
        return [sample for sample, _ in self._by_id.values()]

    def path_for(self, dataset_id: str) -> Path | None:
        entry = self._by_id.get(dataset_id)
        return entry[1] if entry and entry[1].is_file() else None

    def info(self, dataset_id: str) -> DatasetInfo | None:
        entry = self._by_id.get(dataset_id)
        if entry is None:
            return None
        sample, path = entry
        return DatasetInfo(
            dataset_id=sample.dataset_id,
            filename=sample.filename,
            stored_name=path.name,
            size_bytes=sample.size_bytes,
            columns=sample.columns,
            created_at=path.stat().st_mtime,
        )
