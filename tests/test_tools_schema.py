"""Integration tests for the schema-introspection tools."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest

from mssql_mcp.config import Settings
from mssql_mcp.db import Database
from mssql_mcp.tools import indexes as indexes_tools
from mssql_mcp.tools import schema as schema_tools

pytestmark = pytest.mark.skipif(
    os.environ.get("MSSQL_INTEGRATION_TESTS") != "1",
    reason="Set MSSQL_INTEGRATION_TESTS=1 to run integration tests "
    "against a live SQL Server",
)


@pytest.fixture(scope="module")
def db() -> Database:
    return Database(Settings())  # type: ignore[call-arg]


@pytest.fixture
def fixture_table(db: Database) -> Iterator[tuple[str, str]]:
    """Provision a tiny table with a primary key and a non-clustered index."""
    schema = "dbo"
    table = f"mcp_test_{uuid.uuid4().hex[:8]}"
    if db.mode.value == "read_only":
        pytest.skip("Schema tests need a non-read-only connection to provision")
    with db._cursor() as cur:  # noqa: SLF001
        cur.execute(
            f"CREATE TABLE {schema}.{table} ("
            f"  id INT IDENTITY(1,1) PRIMARY KEY,"
            f"  name NVARCHAR(50) NOT NULL,"
            f"  email NVARCHAR(120) NULL"
            f")"
        )
        cur.execute(
            f"CREATE NONCLUSTERED INDEX IX_{table}_email "
            f"ON {schema}.{table}(email) WHERE email IS NOT NULL"
        )
        cur.connection.commit()
    try:
        yield schema, table
    finally:
        with db._cursor() as cur:  # noqa: SLF001
            cur.execute(f"DROP TABLE {schema}.{table}")
            cur.connection.commit()


def test_list_tables_filters_by_name_pattern(
    db: Database, fixture_table: tuple[str, str]
) -> None:
    _, table = fixture_table
    results = schema_tools.list_tables(db, name_pattern=f"{table[:6]}%")
    names = [t.name for t in results]
    assert table in names


def test_describe_table_returns_primary_key(
    db: Database, fixture_table: tuple[str, str]
) -> None:
    schema, table = fixture_table
    description = schema_tools.describe_table(db, schema=schema, table=table)
    assert description.primary_key == ["id"]
    col_names = {c.name for c in description.columns}
    assert {"id", "name", "email"} <= col_names


def test_list_indexes_distinguishes_clustered_from_non_clustered(
    db: Database, fixture_table: tuple[str, str]
) -> None:
    schema, table = fixture_table
    indexes = indexes_tools.list_indexes(db, schema=schema, table=table)
    types = {idx.type for idx in indexes}
    assert "CLUSTERED" in types  # the PK creates a clustered index
    assert "NONCLUSTERED" in types  # the filtered email index
