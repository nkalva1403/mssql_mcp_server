"""Tests for the ``init`` wizard and ``doctor`` subcommand."""

from __future__ import annotations

import argparse
import io
from pathlib import Path
from typing import Any

import pytest

from mssql_mcp import cli


def _run_init(
    inputs: list[str], env_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[int, str]:
    """Drive cmd_init with a scripted stdin and capture stdout."""
    monkeypatch.setattr(
        cli, "_detect_sql_drivers", lambda: ["ODBC Driver 18 for SQL Server"]
    )
    monkeypatch.setattr(cli, "_is_windows", lambda: True)
    stdin = io.StringIO("\n".join(inputs) + "\n")
    stdout = io.StringIO()
    args = argparse.Namespace(env_path=str(env_path))
    code = cli.cmd_init(args, stdin=stdin, stdout=stdout)
    return code, stdout.getvalue()


# --- init wizard ---------------------------------------------------------


def test_init_windows_auth_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = tmp_path / ".env"
    # answers: server (blank -> default), port (blank), database, auth = 1 (windows)
    code, out = _run_init(
        ["", "", "MyDb", "1"], env_path, monkeypatch
    )
    assert code == 0
    assert env_path.exists()
    body = env_path.read_text("utf-8")
    assert "MSSQL_SERVER=localhost" in body
    assert "MSSQL_DATABASE=MyDb" in body
    assert "MSSQL_AUTH_MODE=windows" in body
    assert "ODBC Driver 18 for SQL Server" in body
    assert "claude_desktop_config.json" in out


def test_init_sql_auth_collects_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = tmp_path / ".env"
    # server, port, database, auth=2 (sql), username, password
    code, out = _run_init(
        ["db01", "1433", "Sales", "2", "sa", "hunter2"],
        env_path,
        monkeypatch,
    )
    assert code == 0
    body = env_path.read_text("utf-8")
    assert "MSSQL_AUTH_MODE=sql" in body
    assert "MSSQL_USERNAME=sa" in body
    assert "MSSQL_PASSWORD=hunter2" in body
    # Claude Desktop snippet should include the credentials too
    assert "hunter2" in out


def test_init_service_principal_collects_three_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = tmp_path / ".env"
    # server, port, db, auth=4 (entra_service_principal), tenant, client, secret
    code, _ = _run_init(
        [
            "azuresvr.database.windows.net",
            "1433",
            "Prod",
            "4",
            "tenant-x",
            "client-x",
            "secret-x",
        ],
        env_path,
        monkeypatch,
    )
    assert code == 0
    body = env_path.read_text("utf-8")
    assert "MSSQL_AUTH_MODE=entra_service_principal" in body
    assert "AZURE_TENANT_ID=tenant-x" in body
    assert "AZURE_CLIENT_ID=client-x" in body
    assert "AZURE_CLIENT_SECRET=secret-x" in body


def test_init_aborts_when_env_exists_and_user_declines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("MSSQL_SERVER=keep\n", encoding="utf-8")
    code, out = _run_init([""], env_path, monkeypatch)  # blank -> default "N"
    assert code == 1
    assert "Aborted" in out
    # File unchanged
    assert env_path.read_text("utf-8") == "MSSQL_SERVER=keep\n"


def test_init_requires_database_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = tmp_path / ".env"
    code, out = _run_init(["", "", ""], env_path, monkeypatch)
    assert code == 1
    assert "database name is required" in out
    assert not env_path.exists()


def test_init_non_windows_hides_windows_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_is_windows", lambda: False)
    monkeypatch.setattr(
        cli, "_detect_sql_drivers", lambda: ["ODBC Driver 18 for SQL Server"]
    )
    env_path = tmp_path / ".env"
    stdin = io.StringIO("\n".join(["", "", "Db", "1"]) + "\n")
    stdout = io.StringIO()
    args = argparse.Namespace(env_path=str(env_path))
    code = cli.cmd_init(args, stdin=stdin, stdout=stdout)
    assert code == 0
    # Choice 1 on non-Windows should be SQL auth, not windows
    body = env_path.read_text("utf-8")
    # Since SQL was selected but no user/pass were provided, fields are blank
    assert "MSSQL_AUTH_MODE=sql" in body


# --- doctor --------------------------------------------------------------


def _doctor_args(**overrides: Any) -> argparse.Namespace:
    base = {"skip_token": False, "skip_db": False}
    base.update(overrides)
    return argparse.Namespace(**base)


def test_doctor_reports_missing_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Force settings to fail
    def raise_(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("MSSQL_SERVER is required")

    monkeypatch.setattr("mssql_mcp.config.get_settings", raise_)
    monkeypatch.setattr(
        cli, "_detect_sql_drivers", lambda: ["ODBC Driver 18 for SQL Server"]
    )
    stdout = io.StringIO()
    code = cli.cmd_doctor(_doctor_args(skip_db=True), stdout=stdout)
    text = stdout.getvalue()
    assert code == 1
    assert "[ FAIL ]  Configuration" in text
    assert "mssql-mcp-server init" in text


def test_doctor_reports_no_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_detect_sql_drivers", lambda: [])
    stdout = io.StringIO()
    code = cli.cmd_doctor(
        _doctor_args(skip_db=True, skip_token=True), stdout=stdout
    )
    text = stdout.getvalue()
    assert code == 1
    assert "[ FAIL ]  Microsoft ODBC SQL Server driver" in text
    assert "learn.microsoft.com" in text


def test_doctor_happy_path_with_skips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """All checks pass when env is set, --skip-db and --skip-token are on."""
    monkeypatch.setenv("MSSQL_SERVER", "localhost")
    monkeypatch.setenv("MSSQL_DATABASE", "Db")
    monkeypatch.setenv("MSSQL_AUTH_MODE", "sql")
    monkeypatch.setenv("MSSQL_USERNAME", "u")
    monkeypatch.setenv("MSSQL_PASSWORD", "p")
    monkeypatch.setenv("MSSQL_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(
        cli, "_detect_sql_drivers", lambda: ["ODBC Driver 18 for SQL Server"]
    )

    stdout = io.StringIO()
    code = cli.cmd_doctor(
        _doctor_args(skip_db=True, skip_token=True), stdout=stdout
    )
    text = stdout.getvalue()
    assert code == 0
    assert "[ PASS ]  Python version" in text
    assert "[ PASS ]  Microsoft ODBC SQL Server driver" in text
    assert "[ PASS ]  Configuration" in text
    assert "[ PASS ]  Audit log writable" in text
    assert "All checks passed." in text


def test_doctor_token_skipped_for_non_entra(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MSSQL_SERVER", "localhost")
    monkeypatch.setenv("MSSQL_DATABASE", "Db")
    monkeypatch.setenv("MSSQL_AUTH_MODE", "sql")
    monkeypatch.setenv("MSSQL_USERNAME", "u")
    monkeypatch.setenv("MSSQL_PASSWORD", "p")
    monkeypatch.setenv("MSSQL_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(
        cli, "_detect_sql_drivers", lambda: ["ODBC Driver 18 for SQL Server"]
    )

    stdout = io.StringIO()
    code = cli.cmd_doctor(_doctor_args(skip_db=True), stdout=stdout)
    text = stdout.getvalue()
    assert code == 0
    assert "[ INFO ]  Entra token acquisition - not an Entra auth mode" in text


# --- dispatch ------------------------------------------------------------


def test_dispatch_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli, "_detect_sql_drivers", lambda: ["ODBC Driver 18 for SQL Server"]
    )
    monkeypatch.setattr(cli, "_is_windows", lambda: True)
    # Use a stub stdin via cli.cmd_init monkeypatching, since dispatch
    # uses sys.stdin by default.
    env_path = tmp_path / ".env"

    captured: dict[str, Any] = {}

    def fake_init(
        args: argparse.Namespace, **kw: Any
    ) -> int:
        captured["env_path"] = args.env_path
        return 0

    monkeypatch.setattr(cli, "cmd_init", fake_init)
    code = cli.dispatch(["init", "--env-path", str(env_path)])
    assert code == 0
    assert captured["env_path"] == str(env_path)


def test_dispatch_doctor(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_doctor(
        args: argparse.Namespace, **kw: Any
    ) -> int:
        captured["skip_db"] = args.skip_db
        captured["skip_token"] = args.skip_token
        return 0

    monkeypatch.setattr(cli, "cmd_doctor", fake_doctor)
    code = cli.dispatch(["doctor", "--skip-db", "--skip-token"])
    assert code == 0
    assert captured == {"skip_db": True, "skip_token": True}


# --- driver detection ----------------------------------------------------


def test_driver_rank_prefers_newer_odbc_drivers() -> None:
    assert cli._driver_rank("ODBC Driver 18 for SQL Server") > cli._driver_rank(
        "ODBC Driver 17 for SQL Server"
    )
    assert cli._driver_rank(
        "ODBC Driver 17 for SQL Server"
    ) > cli._driver_rank("SQL Server")


def test_detect_sql_drivers_sorts_newest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pyodbc

    monkeypatch.setattr(
        pyodbc,
        "drivers",
        lambda: [
            "SQL Server",
            "ODBC Driver 17 for SQL Server",
            "ODBC Driver 18 for SQL Server",
            "MySQL ODBC Driver",  # not a SQL Server driver — filtered out
        ],
    )
    drivers = cli._detect_sql_drivers()
    assert drivers[0] == "ODBC Driver 18 for SQL Server"
    assert drivers[1] == "ODBC Driver 17 for SQL Server"
    assert drivers[2] == "SQL Server"
    assert "MySQL ODBC Driver" not in drivers


# --- BOM stripping -------------------------------------------------------


def test_prompt_strips_leading_utf8_bom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PowerShell's `|` pipe prepends a UTF-8 BOM (U+FEFF) to stdin."""
    stdin = io.StringIO("﻿localhost\n")
    stdout = io.StringIO()
    answer = cli._prompt(stdin, stdout, "Server", "default")
    assert answer == "localhost"
