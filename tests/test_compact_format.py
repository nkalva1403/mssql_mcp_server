"""Tests for the opt-in compact response format on ``execute_query``.

The compression contract: same data, columnar shape, far fewer tokens
because column names aren't repeated per row."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from mssql_mcp.db import FetchResult
from mssql_mcp.models import CompactQueryResult, QueryResult
from mssql_mcp.tools import query as query_tools


def _fake_db(rows: list[dict[str, Any]], columns: list[str]) -> MagicMock:
    """Build a Database double whose .fetch() returns the canned result."""
    db = MagicMock()
    db.fetch.return_value = FetchResult(
        columns=columns,
        rows=rows,
        truncated=False,
        duration_ms=3,
    )
    return db


def test_default_format_returns_dict_rows() -> None:
    db = _fake_db(
        rows=[{"id": 1, "name": "a"}, {"id": 2, "name": "b"}],
        columns=["id", "name"],
    )
    result = query_tools.execute_query(db, "SELECT id, name FROM t")
    assert isinstance(result, QueryResult)
    assert result.format == "dict"
    assert result.rows == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
    assert result.row_count == 2


def test_compact_format_returns_columnar_rows() -> None:
    db = _fake_db(
        rows=[{"id": 1, "name": "a"}, {"id": 2, "name": "b"}],
        columns=["id", "name"],
    )
    result = query_tools.execute_query(
        db, "SELECT id, name FROM t", format="compact"
    )
    assert isinstance(result, CompactQueryResult)
    assert result.format == "compact"
    assert result.columns == ["id", "name"]
    assert result.rows == [[1, "a"], [2, "b"]]
    assert result.row_count == 2


def test_compact_preserves_null_and_column_order() -> None:
    db = _fake_db(
        rows=[{"b": None, "a": 1}, {"a": 2, "b": "x"}],
        columns=["a", "b"],
    )
    result = query_tools.execute_query(
        db, "SELECT a, b FROM t", format="compact"
    )
    # Even though the row dicts have keys in a different order, the
    # compact rows must be ordered by the declared `columns` list.
    assert isinstance(result, CompactQueryResult)
    assert result.rows == [[1, None], [2, "x"]]


def test_compact_empty_result_is_empty() -> None:
    db = _fake_db(rows=[], columns=["id"])
    result = query_tools.execute_query(
        db, "SELECT id FROM t WHERE 1=0", format="compact"
    )
    assert isinstance(result, CompactQueryResult)
    assert result.rows == []
    assert result.row_count == 0


def test_compact_payload_is_smaller_than_dict() -> None:
    """Sanity-check the claimed savings on a realistic shape."""
    columns = [f"col_{i}" for i in range(10)]
    rows = [{c: i for c in columns} for i in range(100)]
    db = _fake_db(rows=rows, columns=columns)

    dict_result = query_tools.execute_query(db, "SELECT * FROM t")
    compact_result = query_tools.execute_query(
        db, "SELECT * FROM t", format="compact"
    )
    dict_size = len(dict_result.model_dump_json())
    compact_size = len(compact_result.model_dump_json())
    # The win is meaningful — compact should be at least 30% smaller
    # on this 100-row × 10-col shape.
    assert compact_size < dict_size * 0.7
