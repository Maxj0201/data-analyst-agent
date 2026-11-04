"""Benchmark task schema and loader.

A task is one YAML file::

    id: l1_ecom_order_count
    difficulty: L1            # L1 | L2 | L3 | ADV
    dataset: data/ecommerce/orders.csv   # or a list of paths, relative to the benchmark root
    question: How many orders ...?
    expected: {...}           # shape depends on difficulty, see Expected
    tags: [ecommerce, count]
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Difficulty = Literal["L1", "L2", "L3", "ADV"]
DIFFICULTIES: tuple[Difficulty, ...] = ("L1", "L2", "L3", "ADV")

DEFAULT_BENCHMARK_ROOT = Path(__file__).resolve().parents[3] / "benchmarks"

# Tag vocabulary (see docs/benchmark.md). Benchmark task files may only use these tags.
# TAXONOMY is the controlled set of capability tags reported in "Accuracy by tag"; DOMAIN_TAGS
# name the dataset (exactly one per task); DESCRIPTOR_TAGS are finer-grained, informal labels.
TAXONOMY: tuple[str, ...] = (
    "join",  # answer needs two or more tables
    "window",  # running totals, LAG/LEAD, rank-within-group
    "time-series",  # trend / period-over-period / per-period series
    "outlier",  # finding anomalies, data errors or planted spikes/drops
    "cohort",  # grouping entities by a start event and tracking them
    "injection",  # instructions planted in data or metadata (indirect prompt injection)
    "pii",  # personal data must not be dumped or exfiltrated
    "write-attempt",  # the request or the data pushes the agent to modify data
)
DOMAIN_TAGS: tuple[str, ...] = ("ecommerce", "saas", "hr", "support", "finance")
DESCRIPTOR_TAGS: tuple[str, ...] = (
    "aggregate",
    "churn",
    "column-name",
    "count",
    "dates",
    "derived-metric",
    "exfiltration",
    "file-name",
    "filter",
    "free-text",
    "group-by",
    "missing-data",
    "mrr",
    "open-ended",
    "prompt-leak",
    "ratio",
    "revenue",
    "root-cause",
    "top-n",
)
KNOWN_TAGS: frozenset[str] = frozenset(TAXONOMY + DOMAIN_TAGS + DESCRIPTOR_TAGS)
ADV_TAXONOMY: frozenset[str] = frozenset({"injection", "pii", "write-attempt"})


class Expected(BaseModel):
    """Ground truth. Which fields are required depends on the task difficulty.

    - L1/L2: ``expected_value`` or ``expected_rows`` (plus ``tolerance``).
    - L3: ``rubric`` (list of must-mention points) and ``pass_threshold``.
    - ADV: ``expected_behavior`` plus optional rule-check fields; may also carry an
      ``expected_value`` / ``expected_rows`` for the legitimate part of the question.
    """

    model_config = ConfigDict(extra="forbid")

    # numeric / tabular answers
    expected_value: float | None = None
    expected_rows: list[dict[str, Any]] | None = None
    tolerance: float = Field(0.01, ge=0, description="relative tolerance")
    abs_tolerance: float = Field(0.0, ge=0)
    order_matters: bool = False

    # L3
    rubric: list[str] | None = None
    pass_threshold: float = Field(1.0, ge=0, le=1)

    # ADV
    expected_behavior: str | None = None
    forbidden_output_patterns: list[str] = Field(default_factory=list)
    forbidden_trace_patterns: list[str] = Field(default_factory=list)
    max_emails: int = 2
    max_phones: int = 2
    allow_write_sql: bool = False


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    difficulty: Difficulty
    dataset: list[str]
    question: str
    expected: Expected
    tags: list[str] = Field(default_factory=list)
    # set by the loader; not part of the YAML
    root: Path | None = Field(default=None, exclude=True)

    @field_validator("dataset", mode="before")
    @classmethod
    def _listify(cls, v: Any) -> Any:
        return [v] if isinstance(v, str) else v

    @model_validator(mode="after")
    def _check_expected(self) -> Task:
        ex = self.expected
        has_answer = ex.expected_value is not None or ex.expected_rows is not None
        if self.difficulty in ("L1", "L2") and not has_answer:
            raise ValueError(f"{self.id}: L1/L2 tasks need expected_value or expected_rows")
        if ex.expected_value is not None and ex.expected_rows is not None:
            raise ValueError(f"{self.id}: set expected_value or expected_rows, not both")
        if self.difficulty == "L3" and not ex.rubric:
            raise ValueError(f"{self.id}: L3 tasks need a non-empty rubric")
        if self.difficulty == "ADV" and not ex.expected_behavior:
            raise ValueError(f"{self.id}: ADV tasks need expected_behavior")
        return self

    def dataset_paths(self) -> list[Path]:
        base = self.root or DEFAULT_BENCHMARK_ROOT
        return [(base / p).resolve() for p in self.dataset]


def load_task(path: Path, root: Path | None = None) -> Task:
    with open(path) as f:
        doc = yaml.safe_load(f)
    task = Task.model_validate(doc)
    task.root = root or _infer_root(path)
    return task


def tag_problems(task: Task) -> list[str]:
    """Violations of the tag vocabulary rules for a benchmark task (empty if none)."""
    problems = [f"unknown tag {t!r}" for t in task.tags if t not in KNOWN_TAGS]
    domains = [t for t in task.tags if t in DOMAIN_TAGS]
    if len(domains) != 1:
        problems.append(f"needs exactly one domain tag from {DOMAIN_TAGS}, got {domains}")
    if len(set(task.tags)) != len(task.tags):
        problems.append("duplicate tags")
    if task.difficulty == "ADV" and not ADV_TAXONOMY.intersection(task.tags):
        problems.append(f"ADV tasks need at least one of {sorted(ADV_TAXONOMY)}")
    return problems


def check_tag_filter(tags: Iterable[str], available: Iterable[str]) -> None:
    """Raise ValueError for a ``--tag`` value no task could match (typo protection)."""
    known = set(KNOWN_TAGS) | set(available)
    for t in tags:
        if t not in known:
            close = difflib.get_close_matches(t, sorted(known), n=3)
            hint = f"; did you mean {', '.join(close)}?" if close else ""
            raise ValueError(
                f"unknown tag {t!r}{hint} (taxonomy: {', '.join(TAXONOMY)}; "
                "run `eval tags` for the full list)"
            )


def _infer_root(path: Path) -> Path:
    # benchmarks/tasks/<LEVEL>/<id>.yaml -> benchmarks/
    for parent in path.resolve().parents:
        if parent.name == "tasks":
            return parent.parent
    return path.resolve().parent


def load_tasks(
    root: Path = DEFAULT_BENCHMARK_ROOT,
    difficulties: Iterable[str] | None = None,
    tags: Iterable[str] | None = None,
    ids: Iterable[str] | None = None,
    limit: int | None = None,
    match_all_tags: bool = False,
) -> list[Task]:
    """Load tasks under ``root/tasks`` filtered by difficulty, tag and id.

    Tags match any-of by default, all-of with ``match_all_tags``; an unknown tag raises
    ``ValueError`` instead of silently selecting nothing. Results are sorted by (difficulty
    order, id). ``limit`` picks tasks round-robin across difficulty levels, so a small dev run
    still covers every level, and is deterministic.
    """
    tasks_dir = root / "tasks"
    if not tasks_dir.is_dir():
        raise FileNotFoundError(f"no tasks directory at {tasks_dir}")
    diffs = {d.upper() for d in difficulties} if difficulties else None
    tagset = set(tags) if tags else None
    idset = set(ids) if ids else None

    tasks = [load_task(p, root) for p in sorted(tasks_dir.rglob("*.yaml"))]
    seen: set[str] = set()
    for t in tasks:
        if t.id in seen:
            raise ValueError(f"duplicate task id {t.id}")
        seen.add(t.id)
    if tagset:
        check_tag_filter(tagset, (tag for t in tasks for tag in t.tags))

    def tag_ok(t: Task) -> bool:
        if tagset is None:
            return True
        return tagset.issubset(t.tags) if match_all_tags else bool(tagset.intersection(t.tags))

    out = [
        t
        for t in tasks
        if (diffs is None or t.difficulty in diffs)
        and tag_ok(t)
        and (idset is None or t.id in idset)
    ]
    out.sort(key=lambda t: (DIFFICULTIES.index(t.difficulty), t.id))
    if limit is None or limit >= len(out):
        return out
    return _stratified(out, limit)


def _stratified(tasks: list[Task], limit: int) -> list[Task]:
    """Pick ``limit`` tasks round-robin across difficulties so cheap dev runs see every level."""
    buckets = {d: [t for t in tasks if t.difficulty == d] for d in DIFFICULTIES}
    picked: list[Task] = []
    i = 0
    while len(picked) < limit:
        for d in DIFFICULTIES:
            if i < len(buckets[d]) and len(picked) < limit:
                picked.append(buckets[d][i])
        i += 1
    picked.sort(key=lambda t: (DIFFICULTIES.index(t.difficulty), t.id))
    return picked
