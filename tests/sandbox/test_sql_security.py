"""Security tests for the DuckDB runner: every non-read-only attempt must come back as an
error result (never raise) and must leave the data untouched."""

from __future__ import annotations

import pytest

from analyst_agent.sandbox import DuckDBRunner
from analyst_agent.sandbox._errors import kind_of


@pytest.fixture()
def runner(data_files):
    r = DuckDBRunner(data_files, timeout_s=30)
    yield r
    r.close()


def _assert_orders_intact(runner: DuckDBRunner) -> None:
    res = runner.run_sql("SELECT count(*) AS n FROM orders")
    assert res.ok, res.error
    assert res.rows == [{"n": 5}]


# --- statement allowlist -------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "DROP TABLE orders",
        "drop table orders",
        "DELETE FROM orders",
        "DELETE FROM orders WHERE 1=1",
        "UPDATE orders SET amount = 0",
        "INSERT INTO orders VALUES (99, 'x', 1.0, 'x')",
        "CREATE TABLE evil AS SELECT 1",
        "CREATE TEMP TABLE evil AS SELECT 1",
        "ALTER TABLE orders ADD COLUMN pwned INT",
        "TRUNCATE orders",
        "COPY orders TO '/tmp/exfil.csv'",
        "EXPORT DATABASE '/tmp/exfil'",
        "ATTACH '/tmp/other.db' AS other",
        "DETACH data",
        "INSTALL httpfs",
        "LOAD httpfs",
        "PRAGMA database_list",
        "SET enable_external_access = true",
        "RESET lock_configuration",
        "CALL pragma_version()",
        "VACUUM",
        "CHECKPOINT",
        "BEGIN TRANSACTION",
        "PREPARE p AS DELETE FROM orders",
        "CREATE MACRO pwn() AS 1",
        "SUMMARIZE orders",
        "DESCRIBE orders",
        "FROM orders",  # DuckDB FROM-first shorthand is not on the allowlist
        "",
        "   ",
        "-- only a comment",
    ],
)
def test_non_select_statements_rejected(runner, query):
    res = runner.run_sql(query)
    assert not res.ok
    assert kind_of(res.error) == "StatementNotAllowed"
    assert "not allowed" in res.error.lower() or "empty" in res.error.lower()
    _assert_orders_intact(runner)


@pytest.mark.parametrize(
    "query",
    [
        # stacked queries
        "SELECT 1; DROP TABLE orders",
        "SELECT * FROM orders; DELETE FROM orders;",
        "SELECT 1;DROP TABLE orders--",
        # comment tricks to hide the real leading keyword
        "/* SELECT */ DROP TABLE orders",
        "-- SELECT\nDROP TABLE orders",
        "/* harmless */ DELETE FROM orders",
        # data-modifying CTE / nested
        "WITH x AS (SELECT 1) DELETE FROM orders",
        "WITH x AS (SELECT 1) INSERT INTO orders SELECT * FROM orders",
        "(DELETE FROM orders)",
        # EXPLAIN of something that is not a SELECT (EXPLAIN ANALYZE would execute it)
        "EXPLAIN ANALYZE DELETE FROM orders",
        "EXPLAIN DROP TABLE orders",
        "EXPLAIN ANALYZE UPDATE orders SET amount = 0",
        # classic string-break injection pasted in as the whole query
        "SELECT * FROM orders WHERE customer = '' ; DROP TABLE orders; --'",
        "SELECT * FROM orders WHERE customer = 'a' OR 1=1; DELETE FROM orders",
    ],
)
def test_sql_injection_attempts_rejected(runner, query):
    res = runner.run_sql(query)
    assert not res.ok
    assert kind_of(res.error) == "StatementNotAllowed", res.error
    _assert_orders_intact(runner)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM read_text('/etc/hostname')",
        "SELECT * FROM '/etc/passwd'",
        "SELECT * FROM read_parquet('https://example.com/x.parquet')",
        "SELECT * FROM 'https://example.com/data.csv'",
        "SELECT * FROM glob('/root/*')",
        "SELECT * FROM sqlite_scan('/tmp/x.db', 't')",
        "SELECT * FROM query('DROP TABLE orders')",
        "SELECT * FROM query_table('orders') "
        "WHERE 0 = (SELECT count(*) FROM read_csv('/etc/passwd'))",
    ],
)
def test_select_cannot_reach_files_network_or_side_effects(runner, query):
    """Allowed statement type, but the engine itself refuses external access."""
    res = runner.run_sql(query)
    assert not res.ok
    assert kind_of(res.error) in {"PermissionDenied", "QueryError"}, res.error
    assert "root:" not in res.markdown_preview
    _assert_orders_intact(runner)


def test_injection_payload_in_data_is_just_data(runner):
    res = runner.run_sql("SELECT note FROM orders WHERE id = 3")
    assert res.ok
    assert res.rows == [{"note": "'); DROP TABLE orders; --"}]
    _assert_orders_intact(runner)


def test_engine_is_read_only_even_without_allowlist(runner):
    """Defense in depth: bypass the allowlist and talk to the connection directly."""
    import duckdb

    con = runner._conn  # noqa: SLF001 - deliberately bypassing the public API
    writes = ["DROP TABLE orders", "DELETE FROM orders", "INSERT INTO orders SELECT * FROM orders"]
    for stmt in writes:
        with pytest.raises(duckdb.Error):
            con.execute(stmt)
    for stmt in ["SET enable_external_access = true", "SET lock_configuration = false"]:
        with pytest.raises(duckdb.Error):
            con.execute(stmt)
    _assert_orders_intact(runner)


# --- resource limits --------------------------------------------------------------------


def test_long_running_query_times_out(data_files):
    r = DuckDBRunner(data_files, timeout_s=1)
    try:
        res = r.run_sql("SELECT count(*) FROM range(1000000000000) a")
        assert not res.ok
        assert kind_of(res.error) == "Timeout"
        assert res.duration_ms < 10_000
        # runner is still usable afterwards
        assert r.run_sql("SELECT 1 AS x").rows == [{"x": 1}]
    finally:
        r.close()


def test_infinite_recursive_cte_times_out(data_files):
    r = DuckDBRunner(data_files, timeout_s=1, memory_limit="256MB")
    try:
        res = r.run_sql(
            "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT count(*) FROM r"
        )
        assert not res.ok
        assert kind_of(res.error) in {"Timeout", "MemoryLimitExceeded", "QueryError"}
    finally:
        r.close()


def test_huge_cross_join_memory_bounded(data_files):
    r = DuckDBRunner(data_files, timeout_s=5, memory_limit="128MB")
    try:
        res = r.run_sql("SELECT list(a.range) FROM range(100000000) a, range(10) b")
        assert not res.ok
        assert kind_of(res.error) in {"Timeout", "MemoryLimitExceeded", "QueryError"}
    finally:
        r.close()
