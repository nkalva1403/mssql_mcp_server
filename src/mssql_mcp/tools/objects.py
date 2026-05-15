"""Programmable-object inspection tools.

Covers stored procedures, scalar functions, table-valued functions, and
views. Definitions come from ``sys.sql_modules`` — same source SSMS uses
for "Script object as CREATE". Always read-only; not gated by mode."""

from __future__ import annotations

from typing import Any

from mssql_mcp.db import Database
from mssql_mcp.models import ObjectDefinition, ObjectInfo, ObjectKind

# Mapping from sys.objects.type code → our ObjectKind label.
_TYPE_TO_KIND: dict[str, ObjectKind] = {
    "P": "procedure",
    "PC": "procedure",  # CLR procedure
    "FN": "scalar_function",
    "FS": "scalar_function",  # CLR scalar
    "IF": "table_function",  # inline TVF
    "TF": "table_function",  # multi-statement TVF
    "V": "view",
}

_KIND_TO_TYPES: dict[ObjectKind, tuple[str, ...]] = {
    "procedure": ("P", "PC"),
    "scalar_function": ("FN", "FS"),
    "table_function": ("IF", "TF"),
    "view": ("V",),
}


def _list_objects_sql(types: tuple[str, ...]) -> str:
    placeholders = ",".join("?" for _ in types)
    return (
        "SELECT s.name AS [schema], o.name AS [name], o.type AS [type], "
        "       CONVERT(VARCHAR(33), o.create_date, 126) AS created, "
        "       CONVERT(VARCHAR(33), o.modify_date, 126) AS modified "
        "FROM sys.objects o "
        "INNER JOIN sys.schemas s ON s.schema_id = o.schema_id "
        f"WHERE o.type IN ({placeholders}) "
        "  AND o.is_ms_shipped = 0 "
        "  AND (? IS NULL OR s.name = ?) "
        "  AND (? IS NULL OR o.name LIKE ?) "
        "ORDER BY s.name, o.name"
    )


def _list(
    db: Database,
    kind: ObjectKind,
    schema: str | None,
    name_pattern: str | None,
) -> list[ObjectInfo]:
    types = _KIND_TO_TYPES[kind]
    sql = _list_objects_sql(types)
    bound: tuple[Any, ...] = (*types, schema, schema, name_pattern, name_pattern)
    result = db.fetch(sql, bound)
    return [
        ObjectInfo.model_validate(
            {
                "schema": str(r["schema"]),
                "name": str(r["name"]),
                "kind": _TYPE_TO_KIND[str(r["type"]).strip()],
                "created": (
                    str(r["created"]) if r.get("created") is not None else None
                ),
                "modified": (
                    str(r["modified"]) if r.get("modified") is not None else None
                ),
            }
        )
        for r in result.rows
    ]


def list_procedures(
    db: Database,
    schema: str | None = None,
    name_pattern: str | None = None,
) -> list[ObjectInfo]:
    """List user stored procedures (excludes Microsoft-shipped). Supports
    optional schema filter and a SQL ``LIKE`` pattern on name."""
    return _list(db, "procedure", schema, name_pattern)


def list_functions(
    db: Database,
    schema: str | None = None,
    name_pattern: str | None = None,
    kind: ObjectKind | None = None,
) -> list[ObjectInfo]:
    """List user functions. ``kind`` narrows to ``scalar_function`` or
    ``table_function``; omit to get both."""
    if kind is None:
        scalars = _list(db, "scalar_function", schema, name_pattern)
        tvfs = _list(db, "table_function", schema, name_pattern)
        merged = scalars + tvfs
        merged.sort(key=lambda o: (o.schema_name, o.name))
        return merged
    if kind not in ("scalar_function", "table_function"):
        raise ValueError(
            "kind must be 'scalar_function' or 'table_function' (or omitted)"
        )
    return _list(db, kind, schema, name_pattern)


def list_views(
    db: Database,
    schema: str | None = None,
    name_pattern: str | None = None,
) -> list[ObjectInfo]:
    """List user views (excludes Microsoft-shipped)."""
    return _list(db, "view", schema, name_pattern)


_DEFINITION_SQL = """
SELECT s.name AS [schema],
       o.name AS [name],
       o.type AS [type],
       m.definition AS definition
FROM sys.sql_modules m
INNER JOIN sys.objects o ON o.object_id = m.object_id
INNER JOIN sys.schemas s ON s.schema_id = o.schema_id
WHERE s.name = ? AND o.name = ?
""".strip()


def get_object_definition(
    db: Database,
    schema: str,
    name: str,
) -> ObjectDefinition | None:
    """Return the CREATE source for a procedure / function / view.

    Returns ``None`` if the object doesn't exist (or isn't a kind whose
    source lives in ``sys.sql_modules``)."""
    result = db.fetch(_DEFINITION_SQL, (schema, name))
    if not result.rows:
        return None
    row = result.rows[0]
    type_code = str(row["type"]).strip()
    kind = _TYPE_TO_KIND.get(type_code)
    if kind is None:
        return None
    definition = str(row["definition"] or "")
    return ObjectDefinition.model_validate(
        {
            "schema": str(row["schema"]),
            "name": str(row["name"]),
            "kind": kind,
            "definition": definition,
            "line_count": definition.count("\n") + (1 if definition else 0),
        }
    )
