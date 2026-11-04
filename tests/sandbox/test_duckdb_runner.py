from __future__ import annotations

import pandas as pd
import pytest

from analyst_agent import sandbox
from analyst_agent.sandbox import DataLoadError, DuckDBRunner
from analyst_agent.sandbox._errors import kind_of


def test_csv_select_returns_dicts_and_markdown(data_files):
    r = DuckDBRunner(data_files)
    res = r.run_sql("SELECT id, amount FROM orders WHERE amount > 10 ORDER BY id")
    assert res.ok and res.error is None
    assert res.columns == ["id", "amount"]
    assert res.rows == [
        {"id": 1, "amount": 10.5},
        {"id": 2, "amount": 20.0},
        {"id": 5, "amount": 99.99},
    ]
    assert res.row_count == 3 and not res.truncated
    assert res.markdown_preview.splitlines()[0] == "| id | amount |"
    r.close()


def test_with_and_explain_allowed(data_files):
    r = DuckDBRunner(data_files)
    res = r.run_sql(
        "WITH big AS (SELECT * FROM orders WHERE amount > 5) SELECT count(*) n FROM big"
    )
    assert res.ok and res.rows == [{"n": 4}]
    assert r.run_sql("EXPLAIN SELECT * FROM orders").ok
    assert r.run_sql("  -- leading comment\n/* x */ (SELECT 1 AS a) UNION ALL (SELECT 2)").ok
    assert r.run_sql("select 1 as a;").ok  # trailing semicolon is still one statement
    r.close()


def test_row_cap_reports_total(data_files):
    r = DuckDBRunner(data_files, row_limit=200)
    res = r.run_sql("SELECT range AS i FROM range(12345)")
    assert res.ok
    assert len(res.rows) == 200
    assert res.row_count == 12345 and res.truncated
    assert res.rows[0] == {"i": 0} and res.rows[-1] == {"i": 199}
    assert "of 12345 rows" in res.markdown_preview
    r.close()


def test_order_preserved_in_capped_rows(data_files):
    r = DuckDBRunner(data_files, row_limit=3)
    res = r.run_sql("SELECT range AS i FROM range(1000) ORDER BY i DESC")
    assert [row["i"] for row in res.rows] == [999, 998, 997]
    r.close()


def test_query_error_is_returned_not_raised(data_files):
    r = DuckDBRunner(data_files)
    res = r.run_sql("SELECT no_such_column FROM orders")
    assert not res.ok and kind_of(res.error) == "QueryError"
    assert "no_such_column" in res.error
    r.close()


def test_markdown_escapes_pipes_and_newlines(data_files):
    r = DuckDBRunner(data_files)
    res = r.run_sql("SELECT 'a|b' || chr(10) || 'c' AS s, NULL AS n")
    assert "a\\|b c" in res.markdown_preview and "NULL" in res.markdown_preview
    r.close()


def test_parquet_and_sqlite_sources(tmp_path, sqlite_file):
    pq = tmp_path / "events.parquet"
    import duckdb

    duckdb.sql("SELECT range AS id FROM range(7)").write_parquet(str(pq))
    r = DuckDBRunner([pq, sqlite_file])
    assert set(r.tables) == {"events", "orders", "customers"}
    assert r.run_sql("SELECT count(*) n FROM events").rows == [{"n": 7}]
    res = r.run_sql(
        "SELECT o.customer, c.tier FROM orders o JOIN customers c ON o.customer = c.name "
        "ORDER BY o.id"
    )
    assert res.ok and res.rows[0] == {"customer": "alice", "tier": "gold"}
    r.close()


def test_hostile_file_names_become_safe_identifiers(tmp_path):
    p = tmp_path / 'x"; DROP TABLE y; --.csv'
    pd.DataFrame({"a": [1]}).to_csv(p, index=False)
    r = DuckDBRunner([p])
    (table,) = r.tables
    assert table.replace("_", "").isalnum()
    assert r.run_sql(f"SELECT a FROM {table}").rows == [{"a": 1}]
    r.close()


def test_duckdb_file_source(duckdb_file):
    r = DuckDBRunner([duckdb_file])
    assert sorted(r.tables) == ["customers", "orders"]  # base tables only, no views
    assert r.run_sql("SELECT count(*) AS n FROM orders").rows == [{"n": 5}]
    r.close()


def test_corrupt_duckdb_file_is_load_error(tmp_path):
    bad = tmp_path / "bad.duckdb"
    bad.write_bytes(b"not a database" * 100)
    with pytest.raises(DataLoadError):
        DuckDBRunner([bad])


def test_load_errors(tmp_path):
    with pytest.raises(DataLoadError):
        DuckDBRunner([tmp_path / "missing.csv"])
    bad = tmp_path / "x.xlsx"
    bad.write_text("nope")
    with pytest.raises(DataLoadError):
        DuckDBRunner([bad])


def test_module_level_api(data_files):
    sandbox.reset()
    assert kind_of(sandbox.run_sql("SELECT 1").error) == "NoData"
    assert sandbox.load_data(data_files) == ["orders"]
    res = sandbox.run_sql("SELECT sum(amount) AS total FROM orders")
    assert isinstance(res, sandbox.SqlResult)
    assert res.ok and res.rows[0]["total"] == pytest.approx(140.74)
    sandbox.reset()
