"""Property tests for the DuckDB runner: the statement allowlist and the row cap."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pandas as pd
import pytest
from hypothesis import given
from hypothesis import strategies as st

from analyst_agent.sandbox import DuckDBRunner
from analyst_agent.sandbox._errors import STATEMENT_NOT_ALLOWED, kind_of
from analyst_agent.sandbox.duckdb_runner import StatementNotAllowed, validate_query

ORDERS = pd.DataFrame({"id": [1, 2, 3], "amount": [10.5, 20.0, 7.25], "note": ["a", "b", "c"]})


@pytest.fixture(scope="module")
def runner(tmp_path_factory: pytest.TempPathFactory) -> Iterator[DuckDBRunner]:
    d: Path = tmp_path_factory.mktemp("sqlprops")
    csv = d / "orders.csv"
    ORDERS.to_csv(csv, index=False)
    r = DuckDBRunner({"orders": csv}, timeout_s=10)
    yield r
    r.close()


def _fingerprint(runner: DuckDBRunner) -> tuple[object, ...]:
    """Everything a write could change: the table list, the row data, and the settings that
    keep the sandbox closed."""
    con = runner._conn
    assert con is not None
    cur = con.cursor()
    try:
        tables = cur.execute(
            "SELECT table_schema, table_name, table_type FROM information_schema.tables "
            "ORDER BY ALL"
        ).fetchall()
        rows = cur.execute("SELECT * FROM orders ORDER BY id").fetchall()
        locked = cur.execute(
            "SELECT name, value FROM duckdb_settings() WHERE name IN "
            "('enable_external_access', 'lock_configuration') ORDER BY name"
        ).fetchall()
        dbs = cur.execute("SELECT database_name FROM duckdb_databases() ORDER BY 1").fetchall()
    finally:
        cur.close()
    return tuple(tables), tuple(rows), tuple(locked), tuple(dbs)


# --------------------------------------------------------------------------- statement grammar

_WRITES = [
    "DROP TABLE orders",
    "DROP TABLE IF EXISTS orders",
    "DELETE FROM orders",
    "DELETE FROM orders WHERE id = 1",
    "UPDATE orders SET amount = 0",
    "INSERT INTO orders VALUES (9, 1.0, 'x')",
    "INSERT INTO orders SELECT * FROM orders",
    "CREATE TABLE evil AS SELECT 1 AS x",
    "CREATE OR REPLACE TABLE orders AS SELECT 1 AS x",
    "CREATE TEMP TABLE evil (x INT)",
    "CREATE VIEW v AS SELECT 1",
    "CREATE MACRO m(a) AS a",
    "CREATE SEQUENCE s",
    "CREATE SCHEMA s2",
    "ALTER TABLE orders ADD COLUMN pwned INT",
    "ALTER TABLE orders RENAME TO gone",
    "TRUNCATE orders",
    "COPY orders TO '/tmp/out.csv'",
    "COPY orders FROM '/etc/passwd'",
    "EXPORT DATABASE '/tmp/x'",
    "ATTACH '/tmp/other.duckdb' AS other",
    "DETACH data",
    "SET enable_external_access = true",
    "SET lock_configuration = false",
    "RESET lock_configuration",
    "PRAGMA enable_profiling",
    "INSTALL httpfs",
    "LOAD httpfs",
    "CALL dbgen(sf = 0.01)",
    "CHECKPOINT",
    "VACUUM",
    "BEGIN TRANSACTION",
    "USE memory",
]
_READS = [
    "SELECT * FROM orders",
    "SELECT count(*) FROM orders",
    "SELECT 1",
    "WITH t AS (SELECT * FROM orders) SELECT sum(amount) FROM t",
    "EXPLAIN SELECT * FROM orders",
]
_PREFIXES = st.sampled_from(["", " ", "\n", "-- c\n", "/* c */", "(", "/* ; DROP */ ", "\t"])
_JOINERS = st.sampled_from([";", "; ", ";\n", " ;", ";--\n", ";/**/"])


def _case(s: str, flips: list[bool]) -> str:
    return (
        "".join(c.swapcase() if flips[i % len(flips)] else c for i, c in enumerate(s))
        if flips
        else s
    )


@st.composite
def write_attempts(draw: st.DrawFn) -> str:
    """Writes disguised in the ways an injected or careless query might try."""
    write = draw(st.sampled_from(_WRITES))
    write = _case(write, draw(st.lists(st.booleans(), max_size=8)))
    read = draw(st.sampled_from(_READS))
    shape = draw(st.integers(0, 6))
    pre = draw(_PREFIXES)
    if shape == 0:
        return pre + write
    if shape == 1:  # stacked after an allowed statement
        return pre + read + draw(_JOINERS) + write
    if shape == 2:  # stacked before an allowed statement
        return pre + write + draw(_JOINERS) + read
    if shape == 3:  # CTE in front of a write (DuckDB parses WITH ... INSERT/DELETE/UPDATE)
        return f"{pre}WITH t AS (SELECT 1) {write}"
    if shape == 4:  # EXPLAIN [ANALYZE] of a write
        return (
            pre
            + draw(st.sampled_from(["EXPLAIN ", "EXPLAIN ANALYZE ", "explain analyse "]))
            + write
        )
    if shape == 5:  # comment-hidden keyword
        return f"/* SELECT */ {write}"
    # random junk around a write
    junk = draw(st.text(alphabet=st.characters(codec="ascii"), max_size=12))
    return pre + junk + draw(_JOINERS) + write


@given(write_attempts())
def test_write_attempts_never_execute(runner: DuckDBRunner, query: str) -> None:
    before = _fingerprint(runner)
    res = runner.run_sql(query)
    assert _fingerprint(runner) == before, f"state changed by {query!r}"
    assert not res.ok, f"write-bearing query succeeded: {query!r}"


_TOKENS = st.one_of(
    st.sampled_from(
        [" ", ";", "(", ")", "'", "--", "/*", "*/", "\n", "SELECT ", "WITH ", "EXPLAIN "]
        + [w.split()[0] + " " for w in _WRITES]
        + ["orders", "1", "*", "FROM ", "INTO ", "TABLE ", "AS ", "x"]
    ),
    st.characters(codec="ascii"),
)


@given(st.lists(_TOKENS, max_size=25).map("".join))
def test_random_text_never_writes(runner: DuckDBRunner, query: str) -> None:
    """Arbitrary token soup: a query either is rejected/fails, or it was a single read."""
    before = _fingerprint(runner)
    res = runner.run_sql(query)
    assert _fingerprint(runner) == before, f"state changed by {query!r}"
    if res.ok:
        con = duckdb.connect(":memory:")
        try:
            validate_query(con, query)  # must have been allowlisted
            stmts = con.extract_statements(query)
        finally:
            con.close()
        assert len(stmts) == 1
        assert stmts[0].type in (duckdb.StatementType.SELECT, duckdb.StatementType.EXPLAIN)


@given(write_attempts())
def test_allowlist_rejects_before_execution(query: str) -> None:
    """Layer 1 alone (no read-only database behind it) refuses every write attempt."""
    con = duckdb.connect(":memory:")
    try:
        with pytest.raises(StatementNotAllowed):
            validate_query(con, query)
    finally:
        con.close()


@given(st.sampled_from(_WRITES))
def test_plain_writes_report_statement_not_allowed(runner: DuckDBRunner, write: str) -> None:
    assert kind_of(runner.run_sql(write).error) == STATEMENT_NOT_ALLOWED


# --------------------------------------------------------------------------- row cap


@given(n=st.integers(0, 2_000), limit=st.integers(1, 400))
def test_row_cap_and_truncated_flag(runner: DuckDBRunner, n: int, limit: int) -> None:
    res = runner.run_sql(f"SELECT range AS i FROM range({n})", row_limit=limit)
    assert res.ok, res.error
    assert res.row_count == n
    assert len(res.rows) == min(n, limit)
    assert res.truncated == (n > limit)
    assert res.truncated == (res.row_count > len(res.rows))
    assert [r["i"] for r in res.rows] == list(range(min(n, limit)))
    if res.truncated:
        assert f"of {n} rows" in res.markdown_preview
    elif n == 0:
        assert "_(0 rows)_" in res.markdown_preview


@given(cols=st.integers(1, 5), n=st.integers(0, 50), limit=st.integers(1, 60))
def test_row_cap_keeps_column_shape(runner: DuckDBRunner, cols: int, n: int, limit: int) -> None:
    select = ", ".join(f"range * {k} AS c{k}" for k in range(cols))
    res = runner.run_sql(f"SELECT {select} FROM range({n})", row_limit=limit)
    assert res.ok, res.error
    assert res.columns == [f"c{k}" for k in range(cols)]
    assert all(list(r) == res.columns for r in res.rows)
    assert len(res.rows) == min(n, limit) and res.row_count == n
