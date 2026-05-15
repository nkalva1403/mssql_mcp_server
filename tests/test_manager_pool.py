"""Tests for the multi-env DatabaseManager pool.

Exercise the new ``db_for`` cache and the contract that
``switch_environment`` leaves previously-built pools alive (so the
``compare_*`` tools can read from two servers without a context switch)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mssql_mcp.config import Settings
from mssql_mcp.environments import EnvironmentRegistry
from mssql_mcp.manager import DatabaseManager


def _registry(tmp_path: Path) -> tuple[Settings, EnvironmentRegistry]:
    payload = {
        "default": "dev",
        "environments": [
            {
                "name": "dev",
                "server": "dev.example.com",
                "default_database": "AppDb",
            },
            {
                "name": "qa",
                "server": "qa.example.com",
                "default_database": "AppDb",
            },
            {
                "name": "prod",
                "server": "prod.example.com",
                "default_database": "AppDb",
            },
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


def _stub_pyodbc(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace pyodbc.connect with a fake; record connection strings."""
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


def test_db_for_caches_one_pool_per_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``db_for`` returns the same Database for repeated calls."""
    _stub_pyodbc(monkeypatch)
    settings, reg = _registry(tmp_path)
    manager = DatabaseManager(settings, registry=reg)

    a1 = manager.db_for("qa")
    a2 = manager.db_for("qa")
    assert a1 is a2  # cached, not rebuilt

    # Different env → different Database
    b = manager.db_for("prod")
    assert b is not a1
    assert "prod.example.com" in b.settings.connection_string()
    assert "qa.example.com" in a1.settings.connection_string()


def test_db_for_caches_per_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Different (env, database) combinations get different cached pools."""
    _stub_pyodbc(monkeypatch)
    settings, reg = _registry(tmp_path)
    manager = DatabaseManager(settings, registry=reg)

    a = manager.db_for("dev")  # uses default_database "AppDb"
    b = manager.db_for("dev", database="OtherDb")
    assert a is not b
    assert "DATABASE=AppDb" in a.settings.connection_string()
    assert "DATABASE=OtherDb" in b.settings.connection_string()

    # Same key → cached
    b2 = manager.db_for("dev", database="OtherDb")
    assert b is b2


def test_switch_environment_keeps_old_pool_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cross-env compare tools rely on previously-touched pools staying
    open through a switch — this is a behavior change from the old
    'close on switch' semantics."""
    _stub_pyodbc(monkeypatch)
    settings, reg = _registry(tmp_path)
    manager = DatabaseManager(settings, registry=reg)

    dev_db = manager.current_db  # default
    qa_db = manager.db_for("qa")

    manager.switch_environment("prod")
    # Both previously-built pools must still be accessible
    assert manager.db_for("dev") is dev_db
    assert manager.db_for("qa") is qa_db


def test_db_for_rejects_legacy_mode(
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
    with pytest.raises(RuntimeError, match="db_for requires registry mode"):
        manager.db_for("dev")


def test_db_for_unknown_environment_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pyodbc(monkeypatch)
    settings, reg = _registry(tmp_path)
    manager = DatabaseManager(settings, registry=reg)
    with pytest.raises(KeyError):
        manager.db_for("staging")


def test_close_all_clears_cached_pools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pyodbc(monkeypatch)
    settings, reg = _registry(tmp_path)
    manager = DatabaseManager(settings, registry=reg)
    manager.db_for("qa")
    manager.db_for("prod")
    assert len(manager._pools) == 3  # dev (default) + qa + prod
    manager.close_all()
    assert len(manager._pools) == 0
