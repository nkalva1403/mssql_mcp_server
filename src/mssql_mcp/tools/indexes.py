"""Index and foreign-key introspection tools."""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, cast

from mssql_mcp.db import Database
from mssql_mcp.models import ForeignKeyInfo, IndexInfo, IndexType

_INDEXES_SQL = """
SELECT
    i.index_id,
    i.name AS index_name,
    i.type_desc,
    i.is_unique,
    i.is_primary_key,
    i.filter_definition
FROM sys.indexes i
INNER JOIN sys.tables t ON t.object_id = i.object_id
INNER JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE s.name = ? AND t.name = ?
  AND i.type_desc <> 'HEAP'
ORDER BY i.index_id
""".strip()

_INDEX_COLUMNS_SQL = """
SELECT
    i.index_id,
    c.name AS column_name,
    ic.is_included_column,
    ic.key_ordinal
FROM sys.indexes i
INNER JOIN sys.index_columns ic
    ON ic.object_id = i.object_id AND ic.index_id = i.index_id
INNER JOIN sys.columns c
    ON c.object_id = ic.object_id AND c.column_id = ic.column_id
INNER JOIN sys.tables t ON t.object_id = i.object_id
INNER JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE s.name = ? AND t.name = ?
ORDER BY i.index_id, ic.is_included_column, ic.key_ordinal
""".strip()

_FRAGMENTATION_SQL = """
SELECT
    ips.index_id,
    ips.avg_fragmentation_in_percent
FROM sys.dm_db_index_physical_stats(
    DB_ID(),
    OBJECT_ID(? + '.' + ?),
    NULL, NULL, 'LIMITED'
) ips
""".strip()


def _coerce_index_type(type_desc: str, is_unique: bool) -> IndexType:
    td = type_desc.upper()
    if td.startswith("CLUSTERED") and "COLUMNSTORE" not in td:
        return "CLUSTERED"
    if td.startswith("NONCLUSTERED") and "COLUMNSTORE" not in td:
        return "NONCLUSTERED"
    if td == "HEAP":
        return "HEAP"
    if "XML" in td:
        return "XML"
    if "SPATIAL" in td:
        return "SPATIAL"
    if is_unique:
        return "UNIQUE"
    return "OTHER"


def list_indexes(
    db: Database, schema: str, table: str
) -> list[IndexInfo]:
    """List non-heap indexes on a table, including key columns, included
    columns, filter definitions, and (when available) fragmentation %."""
    indexes_result = db.fetch(_INDEXES_SQL, (schema, table))
    cols_result = db.fetch(_INDEX_COLUMNS_SQL, (schema, table))
    frag_rows: list[dict[str, Any]] = []
    try:
        frag_result = db.fetch(_FRAGMENTATION_SQL, (schema, table))
        frag_rows = frag_result.rows
    except Exception:  # noqa: BLE001 - DMV may be denied; degrade gracefully
        frag_rows = []

    frag_by_id: dict[int, float] = {
        int(r["index_id"]): float(r["avg_fragmentation_in_percent"])
        for r in frag_rows
        if r.get("avg_fragmentation_in_percent") is not None
    }

    keys_by_index: dict[int, list[str]] = {}
    includes_by_index: dict[int, list[str]] = {}
    for r in cols_result.rows:
        idx_id = int(r["index_id"])
        col = str(r["column_name"])
        if bool(r["is_included_column"]):
            includes_by_index.setdefault(idx_id, []).append(col)
        else:
            keys_by_index.setdefault(idx_id, []).append(col)

    out: list[IndexInfo] = []
    for r in indexes_result.rows:
        idx_id = int(r["index_id"])
        out.append(
            IndexInfo(
                name=str(r["index_name"] or f"index_{idx_id}"),
                type=_coerce_index_type(
                    str(r["type_desc"]), bool(r["is_unique"])
                ),
                is_unique=bool(r["is_unique"]),
                is_primary_key=bool(r["is_primary_key"]),
                key_columns=keys_by_index.get(idx_id, []),
                included_columns=includes_by_index.get(idx_id, []),
                filter_definition=(
                    str(r["filter_definition"])
                    if r["filter_definition"] is not None
                    else None
                ),
                fragmentation_pct=frag_by_id.get(idx_id),
            )
        )
    return out


_FK_SQL = """
SELECT
    fk.name AS fk_name,
    direction,
    sp.name AS from_schema,
    tp.name AS from_table,
    cp.name AS from_column,
    sr.name AS to_schema,
    tr.name AS to_table,
    cr.name AS to_column,
    fk.delete_referential_action_desc AS on_delete,
    fk.update_referential_action_desc AS on_update,
    fkc.constraint_column_id
FROM (
    SELECT object_id, 'outgoing' AS direction FROM sys.foreign_keys
    WHERE parent_object_id = OBJECT_ID(? + '.' + ?)
    UNION ALL
    SELECT object_id, 'incoming' FROM sys.foreign_keys
    WHERE referenced_object_id = OBJECT_ID(? + '.' + ?)
) AS fkx
INNER JOIN sys.foreign_keys fk ON fk.object_id = fkx.object_id
INNER JOIN sys.foreign_key_columns fkc
    ON fkc.constraint_object_id = fk.object_id
INNER JOIN sys.tables tp ON tp.object_id = fk.parent_object_id
INNER JOIN sys.schemas sp ON sp.schema_id = tp.schema_id
INNER JOIN sys.tables tr ON tr.object_id = fk.referenced_object_id
INNER JOIN sys.schemas sr ON sr.schema_id = tr.schema_id
INNER JOIN sys.columns cp
    ON cp.object_id = fk.parent_object_id
   AND cp.column_id = fkc.parent_column_id
INNER JOIN sys.columns cr
    ON cr.object_id = fk.referenced_object_id
   AND cr.column_id = fkc.referenced_column_id
ORDER BY direction, fk.name, fkc.constraint_column_id
""".strip()


def list_foreign_keys(
    db: Database, schema: str, table: str
) -> list[ForeignKeyInfo]:
    """List foreign keys touching the given table, in both directions."""
    result = db.fetch(_FK_SQL, (schema, table, schema, table))
    grouped: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()

    for r in result.rows:
        key = (str(r["fk_name"]), str(r["direction"]))
        entry = grouped.setdefault(
            key,
            {
                "name": str(r["fk_name"]),
                "direction": str(r["direction"]),
                "from_schema": str(r["from_schema"]),
                "from_table": str(r["from_table"]),
                "from_columns": [],
                "to_schema": str(r["to_schema"]),
                "to_table": str(r["to_table"]),
                "to_columns": [],
                "on_delete": (
                    str(r["on_delete"]) if r["on_delete"] is not None else None
                ),
                "on_update": (
                    str(r["on_update"]) if r["on_update"] is not None else None
                ),
            },
        )
        from_cols = cast(list[str], entry["from_columns"])
        to_cols = cast(list[str], entry["to_columns"])
        from_cols.append(str(r["from_column"]))
        to_cols.append(str(r["to_column"]))

    return [ForeignKeyInfo(**entry) for entry in grouped.values()]
