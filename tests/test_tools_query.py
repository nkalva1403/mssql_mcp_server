"""Integration tests for the query-execution tools.

Require ``MSSQL_INTEGRATION_TESTS=1`` and a reachable SQL Server.
"""

from __future__ import annotations

import os

import pytest

from mssql_mcp.config import Settings
from mssql_mcp.db import Database, DatabaseError
from mssql_mcp.tools import query as query_tools

pytestmark = pytest.mark.skipif(
    os.environ.get("MSSQL_INTEGRATION_TESTS") != "1",
    reason="Set MSSQL_INTEGRATION_TESTS=1 to run integration tests "
    "against a live SQL Server",
)


@pytest.fixture(scope="module")
def db() -> Database:
    return Database(Settings())  # type: ignore[call-arg]


def test_happy_path_select(db: Database) -> None:
    """A simple SELECT returns the expected columns and rows."""
    result = query_tools.execute_query(
        db, "SELECT 1 AS a, 'x' AS b"
    )
    assert result.columns == ["a", "b"]
    assert result.row_count == 1
    assert result.rows == [{"a": 1, "b": "x"}]
    assert result.truncated is False


def test_max_rows_cap_is_enforced(db: Database) -> None:
    """If a query would return more rows than ``MAX_ROWS``, ``truncated``
    is set and only ``MAX_ROWS`` rows come back."""
    cap = db.settings.mssql_max_rows
    sql = (
        "WITH n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < ?) "
        "SELECT i FROM n OPTION (MAXRECURSION 0)"
    )
    result = query_tools.execute_query(
        db,
        sql,
        params=[
            {"name": "limit", "value": cap + 50, "sql_type": "int"}  # type: ignore[list-item]
        ],
    )
    assert result.row_count == cap
    assert result.truncated is True


def test_query_timeout_fires_on_slow_query(db: Database) -> None:
    """``WAITFOR DELAY '00:00:05'`` against a 1-second timeout must raise."""
    original_timeout = db.settings.mssql_query_timeout
    try:
        db.settings.__dict__["mssql_query_timeout"] = 1
        with pytest.raises(DatabaseError):
            query_tools.execute_query(db, "WAITFOR DELAY '00:00:05'")
    finally:
        db.settings.__dict__["mssql_query_timeout"] = original_timeout


def test_null_round_trips_cleanly(db: Database) -> None:
    result = query_tools.execute_query(
        db, "SELECT CAST(NULL AS NVARCHAR(10)) AS nullable, 2 AS two"
    )
    assert result.rows[0] == {"nullable": None, "two": 2}


def test_single_quote_in_parameter_does_not_break(db: Database) -> None:
    """Bound parameter values may contain single quotes (``'``) and the
    classifier+driver must handle that without inlining or escaping
    breaking the query."""
    result = query_tools.execute_query(
        db,
        "SELECT ? AS name",
        params=[
            {"name": "n", "value": "O'Brien", "sql_type": "nvarchar"}  # type: ignore[list-item]
        ],
    )
    assert result.rows[0]["name"] == "O'Brien"


def test_server_info_reports_mode(db: Database) -> None:
    info = query_tools.server_info(db)
    assert info.mode in {"read_only", "write", "ddl"}
    assert info.database
    assert info.user
