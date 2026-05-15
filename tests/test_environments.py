"""Tests for the environment registry and the runtime DatabaseManager."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from mssql_mcp.config import Settings
from mssql_mcp.environments import Environment, EnvironmentRegistry
from mssql_mcp.manager import DatabaseManager, NoEnvironmentSelectedError
from mssql_mcp.tools import environments as env_tools

# --- Registry parsing ----------------------------------------------------


def _write_registry(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_registry_loads_minimal(tmp_path: Path) -> None:
    p = _write_registry(
        tmp_path / "envs.json",
        {
            "default": "dev",
            "environments": [
                {
                    "name": "dev",
                    "server": "dev.example.com",
                    "default_database": "master",
                }
            ],
        },
    )
    reg = EnvironmentRegistry.from_path(p)
    assert reg.default == "dev"
    assert reg.names() == ["dev"]
    assert reg.get("dev").port == 1433


def test_registry_rejects_duplicate_names(tmp_path: Path) -> None:
    p = _write_registry(
        tmp_path / "envs.json",
        {
            "environments": [
                {"name": "a", "server": "x"},
                {"name": "a", "server": "y"},
            ]
        },
    )
    with pytest.raises(ValidationError, match="duplicate environment"):
        EnvironmentRegistry.from_path(p)


def test_registry_rejects_unknown_default(tmp_path: Path) -> None:
    p = _write_registry(
        tmp_path / "envs.json",
        {
            "default": "qa",
            "environments": [{"name": "dev", "server": "x"}],
        },
    )
    with pytest.raises(ValidationError, match="not defined"):
        EnvironmentRegistry.from_path(p)


def test_registry_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        EnvironmentRegistry.from_path(tmp_path / "nope.json")


def test_registry_invalid_json_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        EnvironmentRegistry.from_path(p)


def test_registry_get_unknown_raises() -> None:
    reg = EnvironmentRegistry(
        default=None,
        environments=[Environment(name="dev", server="x")],
    )
    with pytest.raises(KeyError, match="not found"):
        reg.get("qa")


# --- Settings validation -------------------------------------------------


def test_settings_require_server_when_no_env_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in list(__import__("os").environ):
        if key.startswith(("MSSQL_", "AZURE_", "MCP_")):
            monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValidationError, match="MSSQL_SERVER"):
        Settings(_env_file=None)


def test_settings_allow_no_server_with_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in list(__import__("os").environ):
        if key.startswith(("MSSQL_", "AZURE_", "MCP_")):
            monkeypatch.delenv(key, raising=False)
    envs = _write_registry(
        tmp_path / "envs.json",
        {"environments": [{"name": "dev", "server": "x"}]},
    )
    s = Settings(
        _env_file=None,
        mssql_environments_file=envs,
    )
    assert s.mssql_server is None
    assert s.mssql_database is None
    assert s.mssql_environments_file == envs


# --- DatabaseManager: legacy mode ----------------------------------------


def test_manager_legacy_mode_uses_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pyodbc

    class _FakeConn:
        timeout = 0

        def cursor(self) -> object:
            raise AssertionError("not called")

        def close(self) -> None:
            pass

    monkeypatch.setattr(pyodbc, "connect", lambda *_a, **_kw: _FakeConn())
    settings = Settings(
        _env_file=None,
        mssql_server="legacy.example.com",
        mssql_database="LegacyDb",
        mssql_auth_mode="sql",
        mssql_username="u",
        mssql_password="p",
    )
    manager = DatabaseManager(settings)
    assert manager.current_db is not None
    assert manager.current_env_name is None
    assert manager.current_database_name == "LegacyDb"
    assert manager.current_environment is None


# --- DatabaseManager: registry mode --------------------------------------


def _registry_settings(tmp_path: Path) -> tuple[Settings, EnvironmentRegistry]:
    payload = {
        "default": "dev",
        "environments": [
            {
                "name": "dev",
                "description": "Dev MI",
                "server": "dev.example.com",
                "default_database": "master",
            },
            {
                "name": "qa",
                "server": "qa.example.com",
                "default_database": "AppDb",
            },
        ],
    }
    p = _write_registry(tmp_path / "envs.json", payload)
    reg = EnvironmentRegistry.from_path(p)
    settings = Settings(
        _env_file=None,
        mssql_environments_file=p,
        mssql_auth_mode="sql",
        mssql_username="u",
        mssql_password="p",
    )
    return settings, reg


def _stub_pyodbc(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace pyodbc.connect with a fake that records the connection string."""
    import pyodbc

    seen: list[str] = []

    class _FakeConn:
        timeout = 0

        def cursor(self) -> object:
            raise AssertionError("not called")

        def close(self) -> None:
            pass

    def fake_connect(connstr: str, *_a: object, **_kw: object) -> _FakeConn:
        seen.append(connstr)
        return _FakeConn()

    monkeypatch.setattr(pyodbc, "connect", fake_connect)
    return seen


def test_manager_starts_at_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pyodbc(monkeypatch)
    settings, reg = _registry_settings(tmp_path)
    manager = DatabaseManager(settings, registry=reg)
    assert manager.current_env_name == "dev"
    assert manager.current_database_name == "master"
    assert manager.current_db is not None


def test_manager_no_default_means_no_active_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pyodbc(monkeypatch)
    payload = {
        "environments": [
            {"name": "dev", "server": "dev.example.com", "default_database": "x"}
        ]
    }
    p = _write_registry(tmp_path / "envs.json", payload)
    reg = EnvironmentRegistry.from_path(p)
    settings = Settings(
        _env_file=None,
        mssql_environments_file=p,
        mssql_auth_mode="sql",
        mssql_username="u",
        mssql_password="p",
    )
    manager = DatabaseManager(settings, registry=reg)
    assert manager.current_env_name is None
    with pytest.raises(NoEnvironmentSelectedError):
        _ = manager.current_db


def test_manager_switch_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pyodbc(monkeypatch)
    settings, reg = _registry_settings(tmp_path)
    manager = DatabaseManager(settings, registry=reg)
    manager.switch_environment("qa")
    assert manager.current_env_name == "qa"
    assert manager.current_database_name == "AppDb"
    # The new Database's connection string must point at qa
    cs = manager.current_db.settings.connection_string()
    assert "qa.example.com" in cs
    assert "DATABASE=AppDb" in cs


def test_manager_switch_environment_with_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pyodbc(monkeypatch)
    settings, reg = _registry_settings(tmp_path)
    manager = DatabaseManager(settings, registry=reg)
    manager.switch_environment("qa", database="Reports")
    assert manager.current_database_name == "Reports"
    cs = manager.current_db.settings.connection_string()
    assert "DATABASE=Reports" in cs


def test_manager_switch_unknown_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pyodbc(monkeypatch)
    settings, reg = _registry_settings(tmp_path)
    manager = DatabaseManager(settings, registry=reg)
    with pytest.raises(KeyError):
        manager.switch_environment("staging")


def test_manager_switch_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pyodbc(monkeypatch)
    settings, reg = _registry_settings(tmp_path)
    manager = DatabaseManager(settings, registry=reg)
    manager.switch_database("OtherDb")
    assert manager.current_database_name == "OtherDb"
    cs = manager.current_db.settings.connection_string()
    assert "DATABASE=OtherDb" in cs


def test_manager_switch_database_requires_env_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pyodbc(monkeypatch)
    payload = {
        "environments": [
            {"name": "dev", "server": "dev.example.com", "default_database": "x"}
        ]
    }
    p = _write_registry(tmp_path / "envs.json", payload)
    reg = EnvironmentRegistry.from_path(p)
    settings = Settings(
        _env_file=None,
        mssql_environments_file=p,
        mssql_auth_mode="sql",
        mssql_username="u",
        mssql_password="p",
    )
    manager = DatabaseManager(settings, registry=reg)
    with pytest.raises(NoEnvironmentSelectedError):
        manager.switch_database("x")


def test_manager_legacy_switch_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_pyodbc(monkeypatch)
    settings = Settings(
        _env_file=None,
        mssql_server="legacy.example.com",
        mssql_database="LegacyDb",
        mssql_auth_mode="sql",
        mssql_username="u",
        mssql_password="p",
    )
    manager = DatabaseManager(settings)
    with pytest.raises(RuntimeError, match="No environments registry"):
        manager.switch_environment("dev")


# --- Environment tools (wrapper layer) -----------------------------------


def test_list_environments_in_registry_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pyodbc(monkeypatch)
    settings, reg = _registry_settings(tmp_path)
    manager = DatabaseManager(settings, registry=reg)
    listed = env_tools.list_environments(manager)
    assert {e.name for e in listed} == {"dev", "qa"}
    by_name = {e.name: e for e in listed}
    assert by_name["dev"].description == "Dev MI"
    assert by_name["qa"].default_database == "AppDb"


def test_list_environments_in_legacy_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_pyodbc(monkeypatch)
    settings = Settings(
        _env_file=None,
        mssql_server="legacy.example.com",
        mssql_database="LegacyDb",
        mssql_auth_mode="sql",
        mssql_username="u",
        mssql_password="p",
    )
    manager = DatabaseManager(settings)
    listed = env_tools.list_environments(manager)
    assert len(listed) == 1
    assert listed[0].name == "default"
    assert listed[0].server == "legacy.example.com"


def test_current_environment_reports_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pyodbc(monkeypatch)
    settings, reg = _registry_settings(tmp_path)
    manager = DatabaseManager(settings, registry=reg)
    cur = env_tools.current_environment(manager)
    assert cur.name == "dev"
    assert cur.server == "dev.example.com"
    assert cur.database == "master"


def test_switch_environment_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pyodbc(monkeypatch)
    settings, reg = _registry_settings(tmp_path)
    manager = DatabaseManager(settings, registry=reg)
    cur = env_tools.switch_environment(manager, "qa")
    assert cur.name == "qa"
    assert cur.database == "AppDb"
