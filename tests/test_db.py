"""Integration tests for :mod:`mssql_mcp.db`.

These require a running SQL Server reachable via the env vars in
:mod:`conftest_integration`. Skipped unless ``MSSQL_INTEGRATION_TESTS=1``.
"""

from __future__ import annotations

import os

import pytest

from mssql_mcp.config import Settings
from mssql_mcp.db import Database, DatabaseError, Mode

pytestmark = pytest.mark.skipif(
    os.environ.get("MSSQL_INTEGRATION_TESTS") != "1",
    reason="Set MSSQL_INTEGRATION_TESTS=1 to run integration tests "
    "against a live SQL Server",
)


@pytest.fixture(scope="module")
def db() -> Database:
    return Database(Settings())  # type: ignore[call-arg]


def test_fetch_select_one(db: Database) -> None:
    result = db.fetch("SELECT 1 AS one")
    assert result.columns == ["one"]
    assert result.rows == [{"one": 1}]
    assert result.truncated is False


def test_fetch_with_parameter(db: Database) -> None:
    result = db.fetch("SELECT ? AS echoed", ("O'Brien",))
    assert result.rows[0]["echoed"] == "O'Brien"


def test_fetch_null_roundtrips(db: Database) -> None:
    result = db.fetch("SELECT CAST(NULL AS NVARCHAR(10)) AS nullable")
    assert result.rows[0]["nullable"] is None


def test_classifier_blocks_at_db_layer(db: Database) -> None:
    with pytest.raises(DatabaseError | ValueError):
        db.fetch("EXEC sp_who")


def test_mode_reflects_settings(db: Database) -> None:
    assert db.mode in {Mode.READ_ONLY, Mode.WRITE, Mode.DDL}
