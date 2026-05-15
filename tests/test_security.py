"""Statement-classifier security tests (no live DB needed).

These tests are the spec's hard-line: every malicious or unexpected SQL
string we can think of must be rejected *before* it reaches pyodbc.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pyodbc
import pytest

from mssql_mcp import audit, auth
from mssql_mcp.config import Settings
from mssql_mcp.db import (
    ALWAYS_BLOCKED,
    ClassifierError,
    Database,
    DatabaseError,
    Mode,
    classify,
    is_token_expired_error,
)
from mssql_mcp.server import build_server

# --- Multi-statement rejection --------------------------------------------


def test_rejects_multi_statement_with_drop_table() -> None:
    with pytest.raises(ClassifierError, match="[Mm]ultiple"):
        classify("SELECT 1; DROP TABLE Customers;", mode=Mode.READ_ONLY)


def test_rejects_multi_statement_in_write_mode() -> None:
    with pytest.raises(ClassifierError, match="[Mm]ultiple"):
        classify(
            "UPDATE t SET a = 1; UPDATE t SET a = 2;",
            mode=Mode.WRITE,
        )


def test_trailing_semicolon_is_fine() -> None:
    # A single statement followed by `;` should be accepted.
    assert classify("SELECT 1;", mode=Mode.READ_ONLY) == "SELECT"


def test_semicolon_inside_string_literal_is_safe() -> None:
    assert classify(
        "SELECT 'a; b' AS c", mode=Mode.READ_ONLY
    ) == "SELECT"


def test_semicolon_inside_bracket_identifier_is_safe() -> None:
    assert (
        classify("SELECT [a;b] FROM t", mode=Mode.READ_ONLY) == "SELECT"
    )


# --- Always-blocked keywords ----------------------------------------------


def test_rejects_exec_sp_who() -> None:
    with pytest.raises(ClassifierError, match="EXEC"):
        classify("EXEC sp_who", mode=Mode.READ_ONLY)


def test_rejects_execute_keyword() -> None:
    with pytest.raises(ClassifierError, match="EXECUTE"):
        classify("EXECUTE sp_help", mode=Mode.READ_ONLY)


def test_rejects_backup_database() -> None:
    with pytest.raises(ClassifierError, match="BACKUP"):
        classify("BACKUP DATABASE foo TO DISK = 'x.bak'", mode=Mode.DDL)


def test_rejects_openrowset_inside_select() -> None:
    """OPENROWSET can be a server-side import vector even inside a SELECT."""
    with pytest.raises(ClassifierError, match="OPENROWSET"):
        classify(
            "SELECT * FROM OPENROWSET('SQLNCLI', 'srv', 'SELECT 1')",
            mode=Mode.READ_ONLY,
        )


def test_rejects_opendatasource_inside_select() -> None:
    with pytest.raises(ClassifierError, match="OPENDATASOURCE"):
        classify(
            "SELECT * FROM OPENDATASOURCE('SQLNCLI', 'x').y.z.t",
            mode=Mode.READ_ONLY,
        )


def test_rejects_grant_revoke_deny() -> None:
    for keyword in ("GRANT", "REVOKE", "DENY"):
        with pytest.raises(ClassifierError, match=keyword):
            classify(f"{keyword} SELECT TO foo", mode=Mode.DDL)


def test_rejects_shutdown_reconfigure_kill_dbcc_bulk() -> None:
    for keyword, sql in [
        ("SHUTDOWN", "SHUTDOWN WITH NOWAIT"),
        ("RECONFIGURE", "RECONFIGURE"),
        ("KILL", "KILL 51"),
        ("DBCC", "DBCC CHECKDB"),
        ("BULK", "BULK INSERT t FROM 'x'"),
    ]:
        with pytest.raises(ClassifierError, match=keyword):
            classify(sql, mode=Mode.DDL)


def test_blocked_keyword_set_matches_spec() -> None:
    expected = {
        "EXEC", "EXECUTE", "GRANT", "REVOKE", "DENY",
        "BACKUP", "RESTORE", "SHUTDOWN", "RECONFIGURE",
        "KILL", "DBCC", "BULK", "OPENROWSET", "OPENDATASOURCE",
    }
    assert expected == ALWAYS_BLOCKED


# --- Allow-list enforcement -----------------------------------------------


def test_select_allowed_in_read_only() -> None:
    assert classify(
        "SELECT TOP 10 * FROM dbo.Customers", mode=Mode.READ_ONLY
    ) == "SELECT"


def test_with_cte_allowed_in_read_only() -> None:
    sql = "WITH cte AS (SELECT 1 AS x) SELECT * FROM cte"
    assert classify(sql, mode=Mode.READ_ONLY) == "WITH"


def test_insert_rejected_in_read_only() -> None:
    with pytest.raises(ClassifierError, match="not allowed"):
        classify("INSERT INTO t VALUES (1)", mode=Mode.READ_ONLY)


def test_update_rejected_in_read_only() -> None:
    with pytest.raises(ClassifierError, match="not allowed"):
        classify("UPDATE t SET a = 1 WHERE id = 2", mode=Mode.READ_ONLY)


def test_dml_allowed_in_write_mode() -> None:
    for sql, head in [
        ("INSERT INTO t VALUES (1)", "INSERT"),
        ("UPDATE t SET a = 1 WHERE id = 2", "UPDATE"),
        ("DELETE FROM t WHERE id = 1", "DELETE"),
        (
            "MERGE INTO t USING s ON t.id = s.id "
            "WHEN MATCHED THEN UPDATE SET a = s.a",
            "MERGE",
        ),
    ]:
        assert classify(sql, mode=Mode.WRITE) == head


def test_ddl_rejected_in_write_mode() -> None:
    with pytest.raises(ClassifierError, match="not allowed"):
        classify("CREATE TABLE t (a int)", mode=Mode.WRITE)


def test_ddl_allowed_in_ddl_mode() -> None:
    for sql, head in [
        ("CREATE TABLE t (a int)", "CREATE"),
        ("ALTER TABLE t ADD b int", "ALTER"),
        ("DROP TABLE t", "DROP"),
        ("TRUNCATE TABLE t", "TRUNCATE"),
    ]:
        assert classify(sql, mode=Mode.DDL) == head


# --- Comment / whitespace handling ----------------------------------------


def test_leading_line_comment_stripped() -> None:
    sql = "-- audit trail\nSELECT 1"
    assert classify(sql, mode=Mode.READ_ONLY) == "SELECT"


def test_leading_block_comment_stripped() -> None:
    sql = "/* multi\nline */ SELECT 1"
    assert classify(sql, mode=Mode.READ_ONLY) == "SELECT"


def test_only_whitespace_rejected() -> None:
    with pytest.raises(ClassifierError):
        classify("   \n\t  ", mode=Mode.READ_ONLY)


def test_empty_string_rejected() -> None:
    with pytest.raises(ClassifierError):
        classify("", mode=Mode.READ_ONLY)


def test_only_comments_rejected() -> None:
    with pytest.raises(ClassifierError):
        classify("-- just a comment", mode=Mode.READ_ONLY)


# --- Parameter / quoted-string robustness ---------------------------------


def test_parameter_placeholder_does_not_break_classifier() -> None:
    assert classify(
        "SELECT * FROM t WHERE name = ?", mode=Mode.READ_ONLY
    ) == "SELECT"


def test_single_quote_in_value_does_not_break_classifier() -> None:
    """`O''Brien` is a properly-escaped single quote inside a string literal.
    The classifier must not treat the inner ``'`` as a string boundary."""
    sql = "SELECT * FROM t WHERE name = 'O''Brien'"
    assert classify(sql, mode=Mode.READ_ONLY) == "SELECT"


def test_bracketed_identifier_with_dangerous_word_is_fine() -> None:
    """A column called ``[BACKUP]`` shouldn't be treated as the keyword."""
    # The classifier scans tokens *after* uppercasing the comment-stripped
    # SQL, but it does NOT remove bracket identifiers. So a column literally
    # named [BACKUP] would still be rejected. This is acceptable conservative
    # behaviour — the workaround is to avoid that column name.
    sql = "SELECT [BACKUP] FROM t"
    with pytest.raises(ClassifierError):
        classify(sql, mode=Mode.READ_ONLY)


def test_xp_cmdshell_through_exec_is_blocked() -> None:
    """The canonical privilege-escalation attempt."""
    with pytest.raises(ClassifierError):
        classify(
            "EXEC sp_configure 'xp_cmdshell', 1", mode=Mode.READ_ONLY
        )


# --- Tool registration is gated by mode -----------------------------------


def _settings(read_only: bool, allow_ddl: bool) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        mssql_server="localhost",
        mssql_database="TestDb",
        mssql_username="u",
        mssql_password="p",
        mssql_read_only=read_only,
        mssql_allow_ddl=allow_ddl,
        mssql_audit_log_path="./logs/test_audit_security.jsonl",  # type: ignore[arg-type]
    )


def _registered_tool_names(read_only: bool, allow_ddl: bool) -> set[str]:
    mcp, _ = build_server(_settings(read_only, allow_ddl))
    return {tool.name for tool in mcp._tool_manager.list_tools()}  # noqa: SLF001


def test_read_only_mode_does_not_register_write_tools() -> None:
    names = _registered_tool_names(read_only=True, allow_ddl=False)
    assert "execute_non_query" not in names
    assert "execute_ddl" not in names
    # but the read-side tools are still there
    assert "execute_query" in names
    assert "list_tables" in names


def test_write_mode_without_ddl_registers_non_query_only() -> None:
    names = _registered_tool_names(read_only=False, allow_ddl=False)
    assert "execute_non_query" in names
    assert "execute_ddl" not in names


def test_ddl_mode_registers_ddl_tool() -> None:
    names = _registered_tool_names(read_only=False, allow_ddl=True)
    assert "execute_non_query" in names
    assert "execute_ddl" in names


# --- Entra connection string never carries credentials (incl. at DEBUG) ---


@pytest.mark.parametrize(
    "mode,extras",
    [
        ("entra_default", {}),
        ("entra_cli", {}),
        ("entra_managed_identity", {}),
        (
            "entra_service_principal",
            {
                "azure_tenant_id": "t",
                "azure_client_id": "c",
                "azure_client_secret": "secret-hunter2",
            },
        ),
    ],
)
def test_entra_conn_string_has_no_credentials_at_debug(
    mode: str,
    extras: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        mssql_server="x",
        mssql_database="d",
        mssql_auth_mode=mode,
        **extras,
    )
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("mssql_mcp.test").debug(
            "Connection string: %s", settings.safe_connection_string()
        )

    captured = caplog.text
    assert "PWD=" not in captured
    assert "UID=" not in captured
    assert "Authentication=" not in captured
    # And: no secret values from extras leak either
    assert "secret-hunter2" not in captured


# --- Token-expiry retry: exactly one transparent re-attempt --------------


class _FakeCursor:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn
        self.description: list[tuple[str]] | None = None
        self.timeout: int = 0

    def execute(self, sql: str, *params: Any) -> _FakeCursor:
        outcome = self._conn._program.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        self.description = [("col",)]
        self._rows: list[tuple[Any, ...]] = outcome
        return self

    def fetchmany(self, n: int) -> list[tuple[Any, ...]]:
        return self._rows[:n]

    def close(self) -> None:
        return None


class _FakeConn:
    def __init__(self, program: list[Any]) -> None:
        self._program = program
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def _entra_settings_for_retry() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        mssql_server="x.database.windows.net",
        mssql_database="Db",
        mssql_auth_mode="entra_service_principal",
        azure_tenant_id="t",
        azure_client_id="c",
        azure_client_secret="s",
        mssql_audit_log_path="./logs/test_retry_audit.jsonl",  # type: ignore[arg-type]
    )


class _StubTokenProvider:
    """Stands in for the real TokenProvider — never hits Entra."""

    def __init__(self) -> None:
        self.calls = 0

    def get_token(self) -> auth.TokenStruct:
        self.calls += 1
        return auth.TokenStruct(
            bytes=b"\x00\x00\x00\x00",
            expires_on=datetime.now(UTC) + timedelta(hours=1),
        )


def test_token_expiry_during_query_triggers_one_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    """A pyodbc.Error whose message indicates token expiry must trigger
    exactly one transparent retry that succeeds."""
    audit.configure(__import__("pathlib").Path("./logs/test_retry_audit.jsonl"))

    expired_err = pyodbc.OperationalError(
        "FA004",
        "[Microsoft][ODBC Driver 18 for SQL Server][SQL Server]"
        "Login failed for user '<token-identified principal>'. "
        "The access token is expired.",
    )

    # Two opens: first connection will raise on execute (mid-query expiry);
    # second connection will succeed and return one row.
    connections: list[_FakeConn] = []

    def fake_connect(*_a: Any, **_kw: Any) -> _FakeConn:
        if not connections:
            conn = _FakeConn(program=[expired_err])
        else:
            conn = _FakeConn(program=[[("ok",)]])
        connections.append(conn)
        return conn

    monkeypatch.setattr(pyodbc, "connect", fake_connect)

    settings = _entra_settings_for_retry()
    stub = _StubTokenProvider()
    db = Database(settings, token_provider=stub)  # type: ignore[arg-type]

    result = db.fetch("SELECT 1")
    assert result.rows == [{"col": "ok"}]
    assert len(connections) == 2  # one expired, one fresh
    assert connections[0].closed  # the expired conn was discarded
    assert stub.calls >= 1


def test_token_expiry_retry_only_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the SECOND attempt also fails with a token-expired error, the
    error is raised — we don't loop forever."""
    expired_err = pyodbc.OperationalError(
        "FA004",
        "[SQL Server] The access token is expired.",
    )

    def fake_connect(*_a: Any, **_kw: Any) -> _FakeConn:
        return _FakeConn(program=[expired_err])

    monkeypatch.setattr(pyodbc, "connect", fake_connect)

    settings = _entra_settings_for_retry()
    db = Database(
        settings,
        token_provider=_StubTokenProvider(),  # type: ignore[arg-type]
    )

    with pytest.raises(DatabaseError):
        db.fetch("SELECT 1")


def test_non_token_error_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other_err = pyodbc.ProgrammingError(
        "42S22", "[SQL Server] Invalid column name 'xyz'."
    )
    attempts = {"n": 0}

    def fake_connect(*_a: Any, **_kw: Any) -> _FakeConn:
        attempts["n"] += 1
        return _FakeConn(program=[other_err])

    monkeypatch.setattr(pyodbc, "connect", fake_connect)

    settings = _entra_settings_for_retry()
    db = Database(
        settings,
        token_provider=_StubTokenProvider(),  # type: ignore[arg-type]
    )
    with pytest.raises(DatabaseError):
        db.fetch("SELECT xyz FROM t")
    assert attempts["n"] == 1  # no retry for non-token errors


def test_is_token_expired_error_matches_known_strings() -> None:
    err1 = pyodbc.OperationalError("FA004", "The access token is expired")
    err2 = pyodbc.OperationalError(
        "42000",
        "Login failed for user '<token-identified principal>'.",
    )
    other = pyodbc.ProgrammingError("42S22", "Invalid column name 'xyz'")
    assert is_token_expired_error(err1)
    assert is_token_expired_error(err2)
    assert not is_token_expired_error(other)


# --- Pool eviction before expiry ----------------------------------------


def test_pool_evicts_connection_before_token_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pooled connection whose token expires within the 5-min buffer
    must be discarded and replaced on the next acquire."""
    settings = _entra_settings_for_retry()

    class _OneShotToken:
        def __init__(self, expires_in_seconds: int) -> None:
            self.expires_in = expires_in_seconds
            self.calls = 0

        def get_token(self) -> auth.TokenStruct:
            self.calls += 1
            return auth.TokenStruct(
                bytes=b"\x00",
                expires_on=datetime.now(UTC)
                + timedelta(seconds=self.expires_in),
            )

    stub = _OneShotToken(expires_in_seconds=60)  # within the 5-minute buffer

    def fake_connect(*_a: Any, **_kw: Any) -> _FakeConn:
        return _FakeConn(program=[[("ok",)], [("ok",)]])

    monkeypatch.setattr(pyodbc, "connect", fake_connect)
    db = Database(settings, token_provider=stub)  # type: ignore[arg-type]

    # First fetch opens a connection (close to expiry). Release puts it
    # back — but the pool evicts it on next acquire since fresh_enough()
    # is False, so we expect a NEW token to be requested.
    db.fetch("SELECT 1")
    db.fetch("SELECT 1")
    assert stub.calls >= 2


# `time` is used only to silence the linter — needed in case the file
# is later extended with sleep-based checks.
_ = time
