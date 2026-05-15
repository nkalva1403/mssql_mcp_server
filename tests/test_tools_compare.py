"""Unit tests for the cross-environment compare_* tools.

Mocks ``get_object_definition`` / ``describe_table`` etc. so the diff
logic can be exercised without a live DB."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from mssql_mcp.config import Settings
from mssql_mcp.environments import EnvironmentRegistry
from mssql_mcp.manager import DatabaseManager
from mssql_mcp.models import ColumnInfo, ObjectDefinition, TableDescription
from mssql_mcp.tools import compare as compare_tools


def _registry(tmp_path: Path) -> tuple[Settings, EnvironmentRegistry]:
    payload = {
        "default": "dev",
        "environments": [
            {"name": "dev", "server": "dev.example", "default_database": "Db"},
            {"name": "qa", "server": "qa.example", "default_database": "Db"},
        ],
    }
    p = tmp_path / "envs.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    reg = EnvironmentRegistry.from_path(p)
    settings = Settings(
        _env_file=None,
        mssql_environments_file=p,
        mssql_auth_mode="sql",
        mssql_username="u",
        mssql_password="p",
    )
    return settings, reg


def _stub_pyodbc(monkeypatch: pytest.MonkeyPatch) -> None:
    import pyodbc

    class _FakeConn:
        timeout = 0

        def cursor(self) -> object:
            raise AssertionError("not called")

        def close(self) -> None:
            pass

    monkeypatch.setattr(pyodbc, "connect", lambda *_a, **_kw: _FakeConn())


def _def(definition: str, name: str = "usp_x", kind: str = "procedure") -> ObjectDefinition:
    return ObjectDefinition.model_validate(
        {
            "schema": "dbo",
            "name": name,
            "kind": kind,
            "definition": definition,
            "line_count": definition.count("\n") + (1 if definition else 0),
        }
    )


def test_compare_procedure_identical_returns_no_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pyodbc(monkeypatch)
    settings, reg = _registry(tmp_path)
    manager = DatabaseManager(settings, registry=reg)

    body = "CREATE PROCEDURE dbo.usp_x AS SELECT 1"
    monkeypatch.setattr(
        compare_tools.objects_tools,
        "get_object_definition",
        lambda _db, _s, _n: _def(body),
    )
    diff = compare_tools.compare_procedure(manager, "dev", "qa", "dbo", "usp_x")
    assert diff.identical is True
    assert diff.unified_diff == ""
    assert diff.a_missing is False
    assert diff.b_missing is False


def test_compare_procedure_produces_unified_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pyodbc(monkeypatch)
    settings, reg = _registry(tmp_path)
    manager = DatabaseManager(settings, registry=reg)

    a_body = "CREATE PROCEDURE dbo.usp_x\nAS\n  SELECT 1\n"
    b_body = "CREATE PROCEDURE dbo.usp_x\nAS\n  SELECT 2\n"

    def fake_def(db: MagicMock, _s: str, _n: str) -> ObjectDefinition:
        cs = db.settings.connection_string()
        return _def(a_body if "dev.example" in cs else b_body)

    monkeypatch.setattr(
        compare_tools.objects_tools, "get_object_definition", fake_def
    )
    diff = compare_tools.compare_procedure(manager, "dev", "qa", "dbo", "usp_x")
    assert diff.identical is False
    assert "-  SELECT 1" in diff.unified_diff
    assert "+  SELECT 2" in diff.unified_diff
    assert "dev: dbo.usp_x" in diff.unified_diff
    assert "qa: dbo.usp_x" in diff.unified_diff


def test_compare_procedure_handles_missing_on_one_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pyodbc(monkeypatch)
    settings, reg = _registry(tmp_path)
    manager = DatabaseManager(settings, registry=reg)

    body = "CREATE PROCEDURE dbo.usp_x AS SELECT 1\n"

    def fake_def(db: MagicMock, _s: str, _n: str) -> ObjectDefinition | None:
        cs = db.settings.connection_string()
        return _def(body) if "dev.example" in cs else None

    monkeypatch.setattr(
        compare_tools.objects_tools, "get_object_definition", fake_def
    )
    diff = compare_tools.compare_procedure(manager, "dev", "qa", "dbo", "usp_x")
    assert diff.a_missing is False
    assert diff.b_missing is True
    assert diff.identical is False


def test_compare_procedure_rejects_wrong_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pyodbc(monkeypatch)
    settings, reg = _registry(tmp_path)
    manager = DatabaseManager(settings, registry=reg)

    monkeypatch.setattr(
        compare_tools.objects_tools,
        "get_object_definition",
        lambda _db, _s, _n: _def("CREATE VIEW dbo.v AS SELECT 1", kind="view"),
    )
    with pytest.raises(ValueError, match="not a procedure"):
        compare_tools.compare_procedure(manager, "dev", "qa", "dbo", "usp_x")


def _col(name: str, **overrides: Any) -> ColumnInfo:
    base: dict[str, Any] = {
        "name": name,
        "data_type": "int",
        "max_length": 4,
        "precision": 10,
        "scale": 0,
        "is_nullable": False,
        "is_identity": False,
        "default": None,
    }
    base.update(overrides)
    return ColumnInfo.model_validate(base)


def _table(schema: str, name: str, columns: list[ColumnInfo]) -> TableDescription:
    return TableDescription.model_validate(
        {
            "schema": schema,
            "name": name,
            "columns": columns,
            "primary_key": [],
            "sample_query": f"SELECT TOP (10) * FROM [{schema}].[{name}];",
        }
    )


def test_compare_table_flags_added_removed_changed_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pyodbc(monkeypatch)
    settings, reg = _registry(tmp_path)
    manager = DatabaseManager(settings, registry=reg)

    a_cols = [_col("id"), _col("name", data_type="nvarchar"), _col("legacy_flag")]
    b_cols = [
        _col("id"),
        _col("name", data_type="varchar"),  # type changed
        _col("created_at", data_type="datetime2"),  # added
        # legacy_flag removed
    ]

    monkeypatch.setattr(
        compare_tools.schema_tools,
        "list_tables",
        lambda _db, schema=None, name_pattern=None: [
            type("T", (), {"name": "Users"})()
        ],
    )
    monkeypatch.setattr(
        compare_tools.schema_tools,
        "describe_table",
        lambda db, s, t: _table(
            s, t, a_cols if "dev.example" in db.settings.connection_string() else b_cols
        ),
    )
    monkeypatch.setattr(
        compare_tools.indexes_tools, "list_indexes", lambda *_a, **_kw: []
    )
    monkeypatch.setattr(
        compare_tools.indexes_tools, "list_foreign_keys", lambda *_a, **_kw: []
    )

    diff = compare_tools.compare_table(manager, "dev", "qa", "dbo", "Users")
    assert diff.identical is False
    kinds_by_col = {d.column: d.kind for d in diff.column_diffs}
    assert kinds_by_col == {
        "created_at": "added",
        "legacy_flag": "removed",
        "name": "changed",
    }


def test_compare_table_returns_identical_when_no_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pyodbc(monkeypatch)
    settings, reg = _registry(tmp_path)
    manager = DatabaseManager(settings, registry=reg)

    cols = [_col("id"), _col("name", data_type="nvarchar")]
    monkeypatch.setattr(
        compare_tools.schema_tools,
        "list_tables",
        lambda *_a, **_kw: [type("T", (), {"name": "Users"})()],
    )
    monkeypatch.setattr(
        compare_tools.schema_tools,
        "describe_table",
        lambda _db, s, t: _table(s, t, cols),
    )
    monkeypatch.setattr(
        compare_tools.indexes_tools, "list_indexes", lambda *_a, **_kw: []
    )
    monkeypatch.setattr(
        compare_tools.indexes_tools, "list_foreign_keys", lambda *_a, **_kw: []
    )

    diff = compare_tools.compare_table(manager, "dev", "qa", "dbo", "Users")
    assert diff.identical is True
    assert diff.column_diffs == []
    assert diff.index_diffs == []
    assert diff.foreign_key_diffs == []


def test_compare_table_handles_missing_on_one_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pyodbc(monkeypatch)
    settings, reg = _registry(tmp_path)
    manager = DatabaseManager(settings, registry=reg)

    # Only exists in dev
    monkeypatch.setattr(
        compare_tools.schema_tools,
        "list_tables",
        lambda db, schema=None, name_pattern=None: (
            [type("T", (), {"name": "Users"})()]
            if "dev.example" in db.settings.connection_string()
            else []
        ),
    )
    diff = compare_tools.compare_table(manager, "dev", "qa", "dbo", "Users")
    assert diff.a_missing is False
    assert diff.b_missing is True
    assert diff.identical is False
