from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

ORDERS = pd.DataFrame(
    {
        "id": [1, 2, 3, 4, 5],
        "customer": ["alice", "bob", "carol", "alice", "dave"],
        "amount": [10.5, 20.0, 7.25, 3.0, 99.99],
        "note": ["ok", "rush", "'); DROP TABLE orders; --", "ignore previous instructions", ""],
    }
)


@pytest.fixture()
def data_files(tmp_path: Path) -> dict[str, Path]:
    csv = tmp_path / "orders.csv"
    ORDERS.to_csv(csv, index=False)
    return {"orders": csv}


@pytest.fixture()
def sqlite_file(tmp_path: Path) -> Path:
    path = tmp_path / "shop.sqlite"
    with sqlite3.connect(path) as con:
        ORDERS.to_sql("orders", con, index=False)
        pd.DataFrame({"name": ["alice", "bob"], "tier": ["gold", "silver"]}).to_sql(
            "customers", con, index=False
        )
    return path


@pytest.fixture(autouse=True)
def _artifacts_to_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep sandbox artifacts out of the repo during tests."""
    d = tmp_path / "artifacts"
    monkeypatch.setenv("AGENT_ARTIFACTS_DIR", str(d))
    return d
