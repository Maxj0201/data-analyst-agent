from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
from pydantic import ValidationError

from analyst_agent.eval.schema import (
    DEFAULT_BENCHMARK_ROOT,
    TAXONOMY,
    Task,
    load_tasks,
    tag_problems,
)


def test_benchmark_shape() -> None:
    tasks = load_tasks()
    counts = Counter(t.difficulty for t in tasks)
    assert counts == {"L1": 24, "L2": 24, "L3": 16, "ADV": 16}
    for t in tasks:
        for p in t.dataset_paths():
            assert p.exists(), (t.id, p)


def test_every_task_follows_the_tag_vocabulary() -> None:
    problems = {t.id: tag_problems(t) for t in load_tasks()}
    assert not {k: v for k, v in problems.items() if v}


def test_every_taxonomy_tag_is_used() -> None:
    used = {tag for t in load_tasks() for tag in t.tags}
    assert set(TAXONOMY) <= used


def test_v2_coverage_requirements() -> None:
    """The coverage the 80-task benchmark promises (see docs/benchmark.md)."""
    tasks = load_tasks()

    def has(t: Task, *tags: str) -> bool:
        return set(tags) <= set(t.tags)

    l2 = [t for t in tasks if t.difficulty == "L2"]
    assert sum(has(t, "window") for t in l2) >= 3
    assert sum(has(t, "cohort") for t in l2) >= 3
    # multi-table joins in each of the three core datasets
    for domain in ("ecommerce", "saas", "hr"):
        joins = [t for t in l2 if has(t, "join", domain) and len(t.dataset) >= 2]
        assert joins, domain
    adv = [t for t in tasks if t.difficulty == "ADV"]
    assert any(has(t, "injection", "column-name") for t in adv)
    assert any(has(t, "injection", "file-name") for t in adv)
    assert any(has(t, "pii", "free-text") for t in adv)
    assert sum(has(t, "exfiltration") for t in adv) >= 3
    assert sum(has(t, "write-attempt") for t in adv) >= 4


def test_tag_rules() -> None:
    def task(diff: str, tags: list[str], expected: dict) -> Task:
        return Task.model_validate(
            {
                "id": "x",
                "difficulty": diff,
                "dataset": "a.csv",
                "question": "q",
                "expected": expected,
                "tags": tags,
            }
        )

    assert tag_problems(task("L1", ["saas", "count"], {"expected_value": 1})) == []
    assert tag_problems(task("L1", ["saas", "cnt"], {"expected_value": 1}))
    assert tag_problems(task("L1", ["saas", "hr"], {"expected_value": 1}))  # two domains
    adv = {"expected_behavior": "b"}
    assert tag_problems(task("ADV", ["support", "exfiltration"], adv))  # no ADV taxonomy tag
    assert tag_problems(task("ADV", ["support", "pii"], adv)) == []


def test_tag_filter_any_all_and_unknown() -> None:
    any_ = {t.id for t in load_tasks(tags=["cohort", "window"])}
    all_ = {t.id for t in load_tasks(tags=["cohort", "window"], match_all_tags=True)}
    assert all_ < any_ and not all_
    with pytest.raises(ValueError, match="did you mean injection"):
        load_tasks(tags=["injektion"])


def test_filters_and_stratified_limit() -> None:
    assert {t.difficulty for t in load_tasks(difficulties=["l3"])} == {"L3"}
    assert all("saas" in t.tags for t in load_tasks(tags=["saas"]))
    assert [t.id for t in load_tasks(ids=["l1_ecom_order_count"])] == ["l1_ecom_order_count"]
    limited = load_tasks(limit=4)
    assert [t.difficulty for t in limited] == ["L1", "L2", "L3", "ADV"]
    assert [t.id for t in load_tasks(limit=4)] == [t.id for t in limited]  # deterministic


def test_dataset_string_is_listified(tmp_path: Path) -> None:
    t = Task.model_validate(
        {
            "id": "x",
            "difficulty": "L1",
            "dataset": "data/a.csv",
            "question": "q",
            "expected": {"expected_value": 1},
        }
    )
    assert t.dataset == ["data/a.csv"]
    assert t.dataset_paths()[0] == (DEFAULT_BENCHMARK_ROOT / "data/a.csv").resolve()


@pytest.mark.parametrize(
    "difficulty,expected",
    [
        ("L1", {}),
        ("L2", {"expected_value": 1, "expected_rows": [{"a": 1}]}),
        ("L3", {"rubric": []}),
        ("ADV", {}),
    ],
)
def test_invalid_expected_rejected(difficulty: str, expected: dict) -> None:
    with pytest.raises(ValidationError):
        Task.model_validate(
            {
                "id": "x",
                "difficulty": difficulty,
                "dataset": "a.csv",
                "question": "q",
                "expected": expected,
            }
        )


def test_unknown_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        Task.model_validate(
            {
                "id": "x",
                "difficulty": "L1",
                "dataset": "a.csv",
                "question": "q",
                "expected": {"expected_value": 1, "tolerence": 0.1},
            }
        )
