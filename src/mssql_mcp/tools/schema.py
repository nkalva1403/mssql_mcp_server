"""Schema introspection tools (always available, read-only)."""

from __future__ import annotations

from typing import cast

from mssql_mcp.db import Database
from mssql_mcp.models import (
    ColumnInfo,
    TableDescription,
    TableInfo,
    TableKind,
)

# These DB-role schemas exist in every user database and aren't useful to
# the model. Exclude them from the default ``list_schemas`` view.
_SYSTEM_SCHEMAS = (
    "sys",
    "INFORMATION_SCHEMA",
    "guest",
    "db_owner",
    "db_accessadmin",
    "db_securityadmin",
    "db_ddladmin",
    "db_backupoperator",
    "db_datareader",
    "db_datawriter",
    "db_denydatareader",
    "db_denydatawriter",
)


def _bracket(identifier: str) -> str:
    """Wrap an identifier in T-SQL brackets, escaping any embedded ``]``."""
    return "[" + identifier.replace("]", "]]") + "]"


def list_databases(db: Database) -> list[str]:
    """List user databases on the server (excludes the four system DBs)."""
    sql = (
        "SELECT name FROM sys.databases "
        "WHERE name NOT IN ('master', 'tempdb', 'model', 'msdb') "
        "ORDER BY name"
    )
    result = db.fetch(sql)
    return [str(r["name"]) for r in result.rows]


def list_schemas(db: Database, database: str | None = None) -> list[str]:
    """List schemas in the current database (or in ``database`` if given).

    ``database`` is validated against ``sys.databases`` before being
    spliced into the FROM clause as a bracketed identifier — we never
    interpolate raw user input."""
    placeholders = ",".join("?" for _ in _SYSTEM_SCHEMAS)

    if database is None:
        sql = (
            f"SELECT name FROM sys.schemas "
            f"WHERE name NOT IN ({placeholders}) ORDER BY name"
        )
        result = db.fetch(sql, _SYSTEM_SCHEMAS)
    else:
        # Validate by parameter — only then is it safe to splice as an identifier.
        check = db.fetch(
            "SELECT 1 AS ok FROM sys.databases WHERE name = ?",
            (database,),
        )
        if not check.rows:
            raise ValueError(
                f"Database {database!r} is not accessible from this connection"
            )
        sql = (
            f"SELECT name FROM {_bracket(database)}.sys.schemas "
            f"WHERE name NOT IN ({placeholders}) ORDER BY name"
        )
        result = db.fetch(sql, _SYSTEM_SCHEMAS)

    return [str(r["name"]) for r in result.rows]


_LIST_TABLES_SQL = """
SELECT
    s.name AS [schema],
    t.name AS [name],
    'BASE TABLE' AS [type],
    COALESCE(SUM(p.row_count), 0) AS row_count_estimate
FROM sys.tables t
INNER JOIN sys.schemas s ON s.schema_id = t.schema_id
LEFT JOIN sys.dm_db_partition_stats p
    ON p.object_id = t.object_id AND p.index_id IN (0, 1)
WHERE (? IS NULL OR s.name = ?)
  AND (? IS NULL OR t.name LIKE ?)
GROUP BY s.name, t.name
UNION ALL
SELECT
    s.name, v.name, 'VIEW', 0
FROM sys.views v
INNER JOIN sys.schemas s ON s.schema_id = v.schema_id
WHERE (? IS NULL OR s.name = ?)
  AND (? IS NULL OR v.name LIKE ?)
ORDER BY [schema], [name]
""".strip()


def list_tables(
    db: Database,
    schema: str | None = None,
    name_pattern: str | None = None,
) -> list[TableInfo]:
    """List tables and views, optionally filtered by schema and/or a
    SQL ``LIKE`` pattern on the object name."""
    bound = (schema, schema, name_pattern, name_pattern) * 2
    result = db.fetch(_LIST_TABLES_SQL, bound)
    return [
        TableInfo.model_validate(
            {
                "schema": str(r["schema"]),
                "name": str(r["name"]),
                "type": cast(TableKind, str(r["type"])),
                "row_count_estimate": int(r["row_count_estimate"] or 0),
            }
        )
        for r in result.rows
    ]


_COLUMNS_SQL = """
SELECT
    c.name AS [name],
    TYPE_NAME(c.user_type_id) AS data_type,
    c.max_length,
    c.precision,
    c.scale,
    c.is_nullable,
    c.is_identity,
    dc.definition AS default_definition
FROM sys.columns c
INNER JOIN sys.tables t ON t.object_id = c.object_id
INNER JOIN sys.schemas s ON s.schema_id = t.schema_id
LEFT JOIN sys.default_constraints dc
    ON dc.parent_object_id = c.object_id AND dc.parent_column_id = c.column_id
WHERE s.name = ? AND t.name = ?
ORDER BY c.column_id
""".strip()

_PRIMARY_KEY_SQL = """
SELECT c.name
FROM sys.indexes i
INNER JOIN sys.index_columns ic
    ON ic.object_id = i.object_id AND ic.index_id = i.index_id
INNER JOIN sys.columns c
    ON c.object_id = ic.object_id AND c.column_id = ic.column_id
INNER JOIN sys.tables t ON t.object_id = i.object_id
INNER JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE i.is_primary_key = 1 AND s.name = ? AND t.name = ?
ORDER BY ic.key_ordinal
""".strip()


def describe_table(
    db: Database, schema: str, table: str
) -> TableDescription:
    """Return columns, primary key, and a sample SELECT for one table."""
    cols_result = db.fetch(_COLUMNS_SQL, (schema, table))
    pk_result = db.fetch(_PRIMARY_KEY_SQL, (schema, table))

    columns = [
        ColumnInfo(
            name=str(r["name"]),
            data_type=str(r["data_type"]),
            max_length=(
                int(r["max_length"]) if r["max_length"] is not None else None
            ),
            precision=(
                int(r["precision"]) if r["precision"] is not None else None
            ),
            scale=int(r["scale"]) if r["scale"] is not None else None,
            is_nullable=bool(r["is_nullable"]),
            is_identity=bool(r["is_identity"]),
            default=(
                str(r["default_definition"])
                if r["default_definition"] is not None
                else None
            ),
        )
        for r in cols_result.rows
    ]
    pk = [str(r["name"]) for r in pk_result.rows]
    sample = f"SELECT TOP (10) * FROM {_bracket(schema)}.{_bracket(table)};"
    return TableDescription.model_validate(
        {
            "schema": schema,
            "name": table,
            "columns": columns,
            "primary_key": pk,
            "sample_query": sample,
        }
    )
