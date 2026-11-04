from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
from pydantic import ValidationError

from analyst_agent.eval.schema import DEFAULT_BENCHMARK_ROOT, Task, load_tasks


def test_starter_benchmark_shape() -> None:
    tasks = load_tasks()
    counts = Counter(t.difficulty for t in tasks)
    assert counts == {"L1": 12, "L2": 12, "L3": 6, "ADV": 10}
    for t in tasks:
        for p in t.dataset_paths():
            assert p.exists(), (t.id, p)


def test_adversarial_tasks_use_support_tickets() -> None:
    adv = load_tasks(difficulties=["ADV"])
    assert all(any(p.name == "support_tickets.csv" for p in t.dataset_paths()) for t in adv)


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
