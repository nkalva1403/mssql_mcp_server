"""Unit tests for :mod:`mssql_mcp.audit`."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mssql_mcp import audit


def _read_lines(log_path: Path) -> list[dict[str, object]]:
    text = log_path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    return [json.loads(line) for line in text.splitlines()]


def test_configure_creates_log_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "dir" / "audit.jsonl"
    audit.configure(target)
    assert target.parent.is_dir()


def test_log_statement_writes_jsonl(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    audit.configure(log_path)

    audit.log_statement(
        tool="execute_query",
        sql="SELECT TOP 10 * FROM dbo.Customers",
        params=("Acme",),
        duration_ms=47,
        rows_returned=10,
        row_cap_hit=False,
        success=True,
    )

    for handler in audit.get_logger().handlers:
        handler.flush()

    rows = _read_lines(log_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["tool"] == "execute_query"
    assert "SELECT" in str(row["sql_preview"])
    assert str(row["sql_hash"]).startswith("sha256:")
    assert row["params_count"] == 1
    assert row["rows_returned"] == 10
    assert row["row_cap_hit"] is False
    assert row["success"] is True
    assert row["error"] is None


def test_log_statement_never_logs_param_values(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    audit.configure(log_path)

    secret = "PII-9999-LEAK"
    audit.log_statement(
        tool="execute_query",
        sql="SELECT * FROM dbo.Customers WHERE Ssn = ?",
        params=(secret,),
        duration_ms=5,
        rows_returned=0,
    )

    for handler in audit.get_logger().handlers:
        handler.flush()

    raw = log_path.read_text(encoding="utf-8")
    assert secret not in raw


def test_sql_preview_is_truncated(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    audit.configure(log_path)

    long_sql = "SELECT " + ("a, " * 400) + "1"
    audit.log_statement(
        tool="execute_query",
        sql=long_sql,
        params=None,
        duration_ms=1,
        rows_returned=0,
    )

    for handler in audit.get_logger().handlers:
        handler.flush()

    rows = _read_lines(log_path)
    preview = str(rows[0]["sql_preview"])
    assert len(preview) <= audit.SQL_PREVIEW_MAX_CHARS
    assert preview.endswith("…")


def test_error_is_sanitised(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    audit.configure(log_path)

    err = "Connection failed at C:\\Users\\bob\\secret\\config.ini"
    audit.log_statement(
        tool="execute_query",
        sql="SELECT 1",
        params=None,
        duration_ms=1,
        success=False,
        error=err,
        level=logging.WARNING,
    )

    for handler in audit.get_logger().handlers:
        handler.flush()

    rows = _read_lines(log_path)
    sanitised = str(rows[0]["error"])
    assert "C:\\Users\\bob" not in sanitised
    assert "<path>" in sanitised


def test_log_handler_is_append_only(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    audit.configure(log_path)
    audit.log_statement(
        tool="execute_query",
        sql="SELECT 1",
        params=None,
        duration_ms=1,
        rows_returned=1,
    )
    audit.log_statement(
        tool="execute_query",
        sql="SELECT 2",
        params=None,
        duration_ms=1,
        rows_returned=1,
    )
    for handler in audit.get_logger().handlers:
        handler.flush()
    rows = _read_lines(log_path)
    assert len(rows) == 2


def test_sql_event_has_event_field(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    audit.configure(log_path)
    audit.log_statement(
        tool="execute_query",
        sql="SELECT 1",
        params=None,
        duration_ms=1,
        rows_returned=1,
    )
    for handler in audit.get_logger().handlers:
        handler.flush()
    rows = _read_lines(log_path)
    assert rows[0]["event"] == "sql_executed"


def test_token_acquired_event_logged(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    audit.configure(log_path)
    expiry = datetime.now(UTC) + timedelta(hours=1)
    audit.log_token_acquired(
        auth_mode="entra_service_principal",
        expires_on=expiry,
        client_id="00000000-0000-0000-0000-aaaaaaaad3f4",
        tenant_id="11111111-2222-3333-4444-555555556789",
    )
    for handler in audit.get_logger().handlers:
        handler.flush()
    rows = _read_lines(log_path)
    row = rows[0]
    assert row["event"] == "token_acquired"
    assert row["auth_mode"] == "entra_service_principal"
    assert row["client_id_suffix"] == "...d3f4"
    assert row["tenant_id_suffix"] == "...6789"
    assert "expires_on" in row


def test_token_acquired_never_logs_the_token(tmp_path: Path) -> None:
    """log_token_acquired() takes no token argument — proving by API that
    we cannot leak it. The expiry value is the only secret-adjacent piece
    of state we keep, and the file body never contains 'token=' or 'eyJ'
    (a hint of a JWT prefix)."""
    log_path = tmp_path / "audit.jsonl"
    audit.configure(log_path)
    audit.log_token_acquired(
        auth_mode="entra_cli",
        expires_on=datetime.now(UTC) + timedelta(hours=1),
        client_id="cid",
    )
    for handler in audit.get_logger().handlers:
        handler.flush()
    body = log_path.read_text("utf-8")
    assert "eyJ" not in body
    assert "Bearer" not in body
    assert "access_token" not in body


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only chmod test")
def test_audit_file_has_0600_on_posix(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    audit.configure(log_path)
    audit.log_statement(
        tool="execute_query",
        sql="SELECT 1",
        params=None,
        duration_ms=1,
        rows_returned=1,
    )
    for handler in audit.get_logger().handlers:
        handler.flush()
    mode = log_path.stat().st_mode & 0o777
    assert mode == 0o600
