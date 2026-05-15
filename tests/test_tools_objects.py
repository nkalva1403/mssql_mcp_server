"""Unit tests for the programmable-object inspection tools.

Mocks the Database layer; verifies the SQL-objects shape mapping and
that ``get_object_definition`` returns ``None`` for unknown objects."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from mssql_mcp.db import FetchResult
from mssql_mcp.tools import objects as objects_tools


def _fake_db_with(rows: list[dict[str, Any]]) -> MagicMock:
    db = MagicMock()
    db.fetch.return_value = FetchResult(
        columns=list(rows[0].keys()) if rows else [],
        rows=rows,
        truncated=False,
        duration_ms=1,
    )
    return db


def test_list_procedures_maps_type_codes() -> None:
    """``P`` → procedure, ``PC`` → procedure (CLR)."""
    db = _fake_db_with(
        [
            {
                "schema": "dbo",
                "name": "usp_a",
                "type": "P",
                "created": "2025-01-01T00:00:00",
                "modified": "2025-02-01T00:00:00",
            },
            {
                "schema": "dbo",
                "name": "usp_b",
                "type": "PC",
                "created": "2025-01-01T00:00:00",
                "modified": None,
            },
        ]
    )
    result = objects_tools.list_procedures(db)
    assert [o.name for o in result] == ["usp_a", "usp_b"]
    assert all(o.kind == "procedure" for o in result)


def test_list_functions_returns_both_kinds_by_default() -> None:
    """When ``kind`` is omitted, both scalar and TVF results merge."""
    db = MagicMock()
    # Two calls (one per kind) — return different rows each time.
    db.fetch.side_effect = [
        FetchResult(
            columns=["schema", "name", "type", "created", "modified"],
            rows=[
                {
                    "schema": "dbo", "name": "fn_x", "type": "FN",
                    "created": None, "modified": None,
                }
            ],
            truncated=False,
            duration_ms=1,
        ),
        FetchResult(
            columns=["schema", "name", "type", "created", "modified"],
            rows=[
                {
                    "schema": "dbo", "name": "tvf_y", "type": "IF",
                    "created": None, "modified": None,
                }
            ],
            truncated=False,
            duration_ms=1,
        ),
    ]
    result = objects_tools.list_functions(db)
    by_name = {o.name: o.kind for o in result}
    assert by_name == {"fn_x": "scalar_function", "tvf_y": "table_function"}


def test_list_views_filters_to_v() -> None:
    db = _fake_db_with(
        [
            {
                "schema": "dbo", "name": "v_users", "type": "V",
                "created": None, "modified": None,
            }
        ]
    )
    result = objects_tools.list_views(db)
    assert len(result) == 1
    assert result[0].kind == "view"


def test_get_object_definition_returns_none_when_missing() -> None:
    db = _fake_db_with([])  # empty result
    assert objects_tools.get_object_definition(db, "dbo", "missing") is None


def test_get_object_definition_populates_line_count() -> None:
    body = "CREATE PROCEDURE dbo.usp_a\nAS\nBEGIN\n  SELECT 1\nEND"
    db = _fake_db_with(
        [
            {
                "schema": "dbo",
                "name": "usp_a",
                "type": "P",
                "definition": body,
            }
        ]
    )
    result = objects_tools.get_object_definition(db, "dbo", "usp_a")
    assert result is not None
    assert result.kind == "procedure"
    assert result.line_count == 5
    assert result.definition == body
