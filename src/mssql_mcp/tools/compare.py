"""Cross-environment comparison tools.

Pull an object's definition from two configured environments and return
a unified diff. For tables, diff is structural (columns / indexes / FKs)
because no single ``CREATE TABLE`` source string exists in
``sys.sql_modules``."""

from __future__ import annotations

import difflib
from typing import Any

from mssql_mcp.manager import DatabaseManager
from mssql_mcp.models import (
    ObjectDiff,
    ObjectKind,
    TableColumnDiff,
    TableDiff,
)
from mssql_mcp.tools import indexes as indexes_tools
from mssql_mcp.tools import objects as objects_tools
from mssql_mcp.tools import schema as schema_tools


def _unified(a: str, b: str, env_a: str, env_b: str, label: str) -> str:
    return "".join(
        difflib.unified_diff(
            a.splitlines(keepends=True),
            b.splitlines(keepends=True),
            fromfile=f"{env_a}: {label}",
            tofile=f"{env_b}: {label}",
            n=3,
        )
    )


def _compare_object(
    manager: DatabaseManager,
    env_a: str,
    env_b: str,
    schema: str,
    name: str,
    expected_kind: ObjectKind,
    database_a: str | None = None,
    database_b: str | None = None,
) -> ObjectDiff:
    """Fetch and diff one programmable object across two environments."""
    db_a = manager.db_for(env_a, database=database_a)
    db_b = manager.db_for(env_b, database=database_b)
    def_a = objects_tools.get_object_definition(db_a, schema, name)
    def_b = objects_tools.get_object_definition(db_b, schema, name)

    a_missing = def_a is None
    b_missing = def_b is None
    text_a = def_a.definition if def_a else ""
    text_b = def_b.definition if def_b else ""

    if def_a is not None and def_a.kind != expected_kind:
        raise ValueError(
            f"Object {schema}.{name} in {env_a!r} is a {def_a.kind}, "
            f"not a {expected_kind}; use the matching compare tool."
        )
    if def_b is not None and def_b.kind != expected_kind:
        raise ValueError(
            f"Object {schema}.{name} in {env_b!r} is a {def_b.kind}, "
            f"not a {expected_kind}; use the matching compare tool."
        )

    identical = (not a_missing and not b_missing) and text_a == text_b
    diff = (
        ""
        if identical
        else _unified(text_a, text_b, env_a, env_b, f"{schema}.{name}")
    )
    return ObjectDiff.model_validate(
        {
            "env_a": env_a,
            "env_b": env_b,
            "schema": schema,
            "name": name,
            "kind": expected_kind,
            "identical": identical,
            "unified_diff": diff,
            "a_line_count": def_a.line_count if def_a else 0,
            "b_line_count": def_b.line_count if def_b else 0,
            "a_missing": a_missing,
            "b_missing": b_missing,
        }
    )


def compare_procedure(
    manager: DatabaseManager,
    env_a: str,
    env_b: str,
    schema: str,
    name: str,
    database_a: str | None = None,
    database_b: str | None = None,
) -> ObjectDiff:
    """Unified diff of one stored procedure across two environments."""
    return _compare_object(
        manager, env_a, env_b, schema, name, "procedure",
        database_a=database_a, database_b=database_b,
    )


def compare_function(
    manager: DatabaseManager,
    env_a: str,
    env_b: str,
    schema: str,
    name: str,
    database_a: str | None = None,
    database_b: str | None = None,
) -> ObjectDiff:
    """Unified diff of one user function (scalar or TVF) across two envs."""
    # We don't know up-front whether it's scalar or table-valued; pick
    # the one that actually exists in env_a (falling back to env_b).
    db_a = manager.db_for(env_a, database=database_a)
    db_b = manager.db_for(env_b, database=database_b)
    probe = (
        objects_tools.get_object_definition(db_a, schema, name)
        or objects_tools.get_object_definition(db_b, schema, name)
    )
    if probe is None:
        # Neither side has it — still return a meaningful empty diff.
        return ObjectDiff.model_validate(
            {
                "env_a": env_a,
                "env_b": env_b,
                "schema": schema,
                "name": name,
                "kind": "scalar_function",
                "identical": False,
                "unified_diff": "",
                "a_line_count": 0,
                "b_line_count": 0,
                "a_missing": True,
                "b_missing": True,
            }
        )
    if probe.kind not in ("scalar_function", "table_function"):
        raise ValueError(
            f"{schema}.{name} is a {probe.kind}, not a function; "
            f"use compare_{probe.kind} instead."
        )
    return _compare_object(
        manager, env_a, env_b, schema, name, probe.kind,
        database_a=database_a, database_b=database_b,
    )


def compare_view(
    manager: DatabaseManager,
    env_a: str,
    env_b: str,
    schema: str,
    name: str,
    database_a: str | None = None,
    database_b: str | None = None,
) -> ObjectDiff:
    """Unified diff of one view's definition across two environments."""
    return _compare_object(
        manager, env_a, env_b, schema, name, "view",
        database_a=database_a, database_b=database_b,
    )


def _column_dict(col: Any) -> dict[str, Any]:
    """Project a ColumnInfo to a dict suitable for equality comparison."""
    return {
        "data_type": col.data_type,
        "max_length": col.max_length,
        "precision": col.precision,
        "scale": col.scale,
        "is_nullable": col.is_nullable,
        "is_identity": col.is_identity,
        "default": col.default,
    }


def _index_dict(idx: Any) -> dict[str, Any]:
    return {
        "name": idx.name,
        "type": idx.type,
        "is_unique": idx.is_unique,
        "is_primary_key": idx.is_primary_key,
        "key_columns": list(idx.key_columns),
        "included_columns": list(idx.included_columns),
        "filter_definition": idx.filter_definition,
    }


def _fk_dict(fk: Any) -> dict[str, Any]:
    return {
        "name": fk.name,
        "direction": fk.direction,
        "from_columns": list(fk.from_columns),
        "to_schema": fk.to_schema,
        "to_table": fk.to_table,
        "to_columns": list(fk.to_columns),
        "on_delete": fk.on_delete,
        "on_update": fk.on_update,
    }


def compare_table(
    manager: DatabaseManager,
    env_a: str,
    env_b: str,
    schema: str,
    table: str,
    database_a: str | None = None,
    database_b: str | None = None,
) -> TableDiff:
    """Structural diff of one table across two environments.

    Compares columns (type/nullability/identity/default), indexes
    (key + included columns, filter), and foreign keys. The result lists
    only the differences — identical fields are omitted."""
    db_a = manager.db_for(env_a, database=database_a)
    db_b = manager.db_for(env_b, database=database_b)

    # Existence probe via list_tables filter.
    a_exists = bool(schema_tools.list_tables(db_a, schema=schema, name_pattern=table))
    b_exists = bool(schema_tools.list_tables(db_b, schema=schema, name_pattern=table))

    if not a_exists and not b_exists:
        return TableDiff.model_validate(
            {
                "env_a": env_a,
                "env_b": env_b,
                "schema": schema,
                "name": table,
                "identical": False,
                "a_missing": True,
                "b_missing": True,
            }
        )
    if not a_exists or not b_exists:
        return TableDiff.model_validate(
            {
                "env_a": env_a,
                "env_b": env_b,
                "schema": schema,
                "name": table,
                "identical": False,
                "a_missing": not a_exists,
                "b_missing": not b_exists,
            }
        )

    desc_a = schema_tools.describe_table(db_a, schema, table)
    desc_b = schema_tools.describe_table(db_b, schema, table)

    cols_a = {c.name: _column_dict(c) for c in desc_a.columns}
    cols_b = {c.name: _column_dict(c) for c in desc_b.columns}
    column_diffs: list[TableColumnDiff] = []
    for cname in sorted(set(cols_a) | set(cols_b)):
        a = cols_a.get(cname)
        b = cols_b.get(cname)
        if a is None:
            column_diffs.append(TableColumnDiff(column=cname, kind="added", b=b))
        elif b is None:
            column_diffs.append(TableColumnDiff(column=cname, kind="removed", a=a))
        elif a != b:
            column_diffs.append(
                TableColumnDiff(column=cname, kind="changed", a=a, b=b)
            )

    idx_a = {i.name: _index_dict(i) for i in indexes_tools.list_indexes(db_a, schema, table)}
    idx_b = {i.name: _index_dict(i) for i in indexes_tools.list_indexes(db_b, schema, table)}
    index_diffs: list[dict[str, Any]] = []
    for iname in sorted(set(idx_a) | set(idx_b)):
        if iname not in idx_a:
            index_diffs.append({"name": iname, "kind": "added", "b": idx_b[iname]})
        elif iname not in idx_b:
            index_diffs.append({"name": iname, "kind": "removed", "a": idx_a[iname]})
        elif idx_a[iname] != idx_b[iname]:
            index_diffs.append({
                "name": iname,
                "kind": "changed",
                "a": idx_a[iname],
                "b": idx_b[iname],
            })

    fk_a = {f.name: _fk_dict(f) for f in indexes_tools.list_foreign_keys(db_a, schema, table)}
    fk_b = {f.name: _fk_dict(f) for f in indexes_tools.list_foreign_keys(db_b, schema, table)}
    fk_diffs: list[dict[str, Any]] = []
    for fname in sorted(set(fk_a) | set(fk_b)):
        if fname not in fk_a:
            fk_diffs.append({"name": fname, "kind": "added", "b": fk_b[fname]})
        elif fname not in fk_b:
            fk_diffs.append({"name": fname, "kind": "removed", "a": fk_a[fname]})
        elif fk_a[fname] != fk_b[fname]:
            fk_diffs.append({
                "name": fname,
                "kind": "changed",
                "a": fk_a[fname],
                "b": fk_b[fname],
            })

    identical = not column_diffs and not index_diffs and not fk_diffs
    return TableDiff.model_validate(
        {
            "env_a": env_a,
            "env_b": env_b,
            "schema": schema,
            "name": table,
            "identical": identical,
            "column_diffs": column_diffs,
            "index_diffs": index_diffs,
            "foreign_key_diffs": fk_diffs,
        }
    )
