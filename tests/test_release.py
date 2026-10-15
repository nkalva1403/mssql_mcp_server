"""Unit tests for the release-batch guard, config lock and result parsing.

No database needed: the guard runs before any connection is opened, and
result parsing is exercised against a fake cursor."""

from __future__ import annotations

from typing import Any

import pytest

from mssql_mcp.environments import Environment, EnvironmentRegistry
from mssql_mcp.release import (
    BatchOutcome,
    ReleaseConfigError,
    ReleaseGuardError,
    build_wrapper,
    check_batch,
    drain_results,
    resolve_release_targets,
    sql_sha256,
    strip_comments_literals_brackets,
)

REGISTRY = EnvironmentRegistry(
    default="dev",
    environments=[
        Environment(name="dev", server="s1", default_database="uw_dev_in_cms_content_db"),
        Environment(name="dev2", server="s1", default_database="uw_dev_in2_cms_content_db"),
        Environment(name="prod", server="s2", default_database="uw_prod_cms_content_db"),
        Environment(
            name="stg",
            server="s3",
            default_database="uw_stg_cms_content_db",
            description="Staging CMS (US) - READ-ONLY",
        ),
    ],
)
DEV2 = REGISTRY.get("dev2")


def _check(sql: str, **kwargs: Any) -> None:
    check_batch(sql, target=DEV2, registry=REGISTRY, **kwargs)


# --- config lock -----------------------------------------------------------


def test_feature_off_when_unset() -> None:
    assert resolve_release_targets(None, REGISTRY) == {}
    assert resolve_release_targets("  ", REGISTRY) == {}


def test_allowed_target_resolves() -> None:
    targets = resolve_release_targets("dev2", REGISTRY)
    assert list(targets) == ["dev2"]


@pytest.mark.parametrize("name", ["prod", "stg", "dev2,prod"])
def test_prod_and_staging_refused_at_startup(name: str) -> None:
    with pytest.raises(ReleaseConfigError, match="prod/staging"):
        resolve_release_targets(name, REGISTRY)


def test_unknown_environment_refused() -> None:
    with pytest.raises(ReleaseConfigError, match="not found"):
        resolve_release_targets("qa9", REGISTRY)


def test_registry_required() -> None:
    with pytest.raises(ReleaseConfigError, match="registry"):
        resolve_release_targets("dev2", None)


# --- batch guard: allowed ----------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE OR ALTER PROCEDURE dbo.SP_CMS_X AS SELECT 1",
        "IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'T')\n"
        "BEGIN CREATE TABLE dbo.T (Id int) END",
        "DECLARE @n int = 1; UPDATE dbo.T SET A = @n WHERE Id = 2;",
        "BEGIN TRAN; DELETE FROM dbo.T WHERE Id = 3; COMMIT;",
        "EXEC sp_rename 'dbo.T.A', 'B', 'COLUMN';",
        "INSERT dbo.Log (Msg) VALUES (N'nightly BACKUP job; USE caution');",
        "SELECT [Use], [Backup] FROM dbo.T -- USE otherdb\n/* RESTORE */",
        "SELECT * FROM dbo.T OPTION (USE HINT('FORCE_LEGACY_CARDINALITY_ESTIMATION'))",
        "DBCC CHECKIDENT ('dbo.T', RESEED, 0)",
        "GRANT EXECUTE ON dbo.SP_CMS_X TO cms_role",
        "SELECT * FROM uw_dev_in2_cms_content_db.dbo.T",
        "UPDATE t SET t.Category = 'GO' FROM dbo.T t",
    ],
)
def test_typical_release_batches_pass(sql: str) -> None:
    _check(sql)


# --- batch guard: refused ----------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("", "empty"),
        ("CREATE TABLE dbo.A (Id int)\nGO\nCREATE TABLE dbo.B (Id int)", "GO separator"),
        ("SELECT 1\n  go 2  \n", "GO separator"),
        ("USE uw_dev_in_cms_content_db;\nUPDATE dbo.T SET A = 1", "USE"),
        ("USE [uw_prod_cms_content_db]", "USE"),
        ("BACKUP DATABASE x TO DISK = 'c:\\x.bak'", "BACKUP"),
        ("RESTORE DATABASE x FROM DISK = 'c:\\x.bak'", "RESTORE"),
        ("ALTER DATABASE CURRENT SET RECOVERY SIMPLE", "database-level"),
        ("DROP DATABASE foo", "database-level"),
        ("CREATE LOGIN bob WITH PASSWORD = 'x'", "database-level"),
        ("EXEC xp_cmdshell 'dir'", "xp_cmdshell"),
        ("SELECT * FROM OPENQUERY(LNK, 'select 1')", "OPENQUERY"),
        ("DBCC SHRINKFILE (1)", "DBCC"),
        ("EXECUTE AS LOGIN = 'sa'", "EXECUTE AS LOGIN"),
        ("SELECT * FROM lnk.db.dbo.T", "four-part"),
        ("UPDATE uw_dev_in_cms_content_db.dbo.T SET A = 1", "another environment"),
        ("DELETE FROM [uw_prod_cms_content_db].[dbo].[T]", "another environment"),
        ("SELECT * FROM uw_stg_cms_content_db..T", "another environment"),
    ],
)
def test_dangerous_batches_blocked(sql: str, reason: str) -> None:
    with pytest.raises(ReleaseGuardError, match=reason):
        _check(sql)


def test_session_settings_whitelist() -> None:
    _check("SELECT 1", session_settings=["SET QUOTED_IDENTIFIER ON", "set ansi_nulls off;"])
    with pytest.raises(ReleaseGuardError, match="session setting"):
        _check("SELECT 1", session_settings=["SET QUOTED_IDENTIFIER ON; DROP TABLE dbo.T"])
    with pytest.raises(ReleaseGuardError, match="session setting"):
        _check("SELECT 1", session_settings=["SET ROWCOUNT 1"])


# --- helpers -----------------------------------------------------------------


def test_strip_removes_comments_literals_and_brackets() -> None:
    out = strip_comments_literals_brackets("SELECT 'it''s' /* a /* b */ c */, [x]]y] -- tail")
    assert "it" not in out and "tail" not in out and "x" in out
    assert "/*" not in out


def test_hash_ignores_line_endings_and_trailing_space() -> None:
    assert sql_sha256("SELECT 1  \r\nFROM t\r\n") == sql_sha256("SELECT 1\nFROM t")


def test_wrapper_places_session_settings_before_try() -> None:
    wrapper = build_wrapper(["SET QUOTED_IDENTIFIER ON"])
    assert wrapper.index("SET QUOTED_IDENTIFIER ON;") < wrapper.index("BEGIN TRY")
    assert "sys.sp_executesql" in wrapper
    assert wrapper.count("?") == 1


class _FakeCursor:
    """Replays a list of (description, rows, rowcount, messages) result sets."""

    def __init__(self, sets: list[tuple[Any, list[tuple[Any, ...]], int, list[Any]]]) -> None:
        self._sets = sets
        self._i = 0

    def _cur(self) -> tuple[Any, list[tuple[Any, ...]], int, list[Any]]:
        return self._sets[self._i]

    @property
    def description(self) -> Any:
        return self._cur()[0]

    @property
    def rowcount(self) -> int:
        return self._cur()[2]

    @property
    def messages(self) -> list[Any]:
        return self._cur()[3]

    def fetchone(self) -> tuple[Any, ...]:
        return self._cur()[1][0]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._cur()[1]

    def nextset(self) -> bool:
        self._i += 1
        return self._i < len(self._sets)


_COLS = [
    ("__rb_status",),
    ("error_number",),
    ("error_severity",),
    ("error_state",),
    ("error_line",),
    ("error_procedure",),
    ("error_message",),
    ("rolled_back",),
    ("db_name",),
]


def _outcome() -> BatchOutcome:
    return BatchOutcome(status="failed", started_at="", duration_ms=0, sql_sha256="")


def test_drain_success_collects_rowcounts_and_messages() -> None:
    cur = _FakeCursor(
        [
            (None, [], 3, [("01000", "[Microsoft][ODBC Driver 18][SQL Server]hello")]),
            ([("Id",)], [(1,), (2,)], -1, []),
            (None, [], 5, []),
            (_COLS, [("succeeded", None, None, None, None, None, None, 0, "db2")], -1, []),
        ]
    )
    out = _outcome()
    drain_results(cur, out)
    assert out.status == "succeeded"
    assert out.rows_affected == [3, 5]
    assert out.result_sets_returned == 1
    assert out.messages == ["hello"]
    assert out.database_after == "db2"


def test_drain_failure_reports_error_details() -> None:
    cur = _FakeCursor(
        [
            (
                _COLS,
                [("failed", 208, 16, 1, 4, None, "Invalid object name 'dbo.Nope'.", 1, "db2")],
                -1,
                [],
            ),
        ]
    )
    out = _outcome()
    drain_results(cur, out)
    assert out.status == "failed"
    assert (out.error_number, out.error_severity, out.error_line) == (208, 16, 4)
    assert out.error_message == "Invalid object name 'dbo.Nope'."
    assert out.transaction_rolled_back is True


# --- tool layer (blocked paths only; no database) ----------------------------


@pytest.fixture
def tool_env(tmp_path: Any) -> tuple[Any, dict[str, Environment], Any]:
    from mssql_mcp.config import Settings
    from mssql_mcp.manager import DatabaseManager

    env_file = tmp_path / "envs.json"
    env_file.write_text(REGISTRY.model_dump_json(), encoding="utf-8")
    root = tmp_path / "runs"
    root.mkdir()
    settings = Settings(  # type: ignore[call-arg]
        mssql_environments_file=env_file,
        mssql_release_environments="dev2",
        mssql_release_batch_root=root,
        mssql_release_journal_dir=tmp_path / "journal",
    )
    manager = DatabaseManager(settings, registry=REGISTRY)
    targets = resolve_release_targets(settings.mssql_release_environments, REGISTRY)
    return manager, targets, tmp_path


def test_batch_file_outside_root_blocked(tool_env: Any) -> None:
    from mssql_mcp.tools.release import run_release_batch

    manager, targets, tmp = tool_env
    outside = tmp / "evil.sql"
    outside.write_text("SELECT 1", encoding="utf-8")
    r = run_release_batch(manager, targets, "dev2", batch_file=str(outside), run_id="r1")
    assert r.status == "blocked" and "under the release batch root" in (r.error_message or "")
    r = run_release_batch(manager, targets, "dev2", batch_file="../evil.sql", run_id="r1")
    assert r.status == "blocked"


def test_batch_file_is_read_and_hashed(tool_env: Any) -> None:
    from mssql_mcp.tools.release import run_release_batch

    manager, targets, tmp = tool_env
    f = tmp / "runs" / "b1.sql"
    f.write_text("USE otherdb\n", encoding="utf-8")  # guard refuses, so nothing runs
    r = run_release_batch(
        manager, targets, "dev2", batch_file="b1.sql", label="F01-B001", run_id="r1"
    )
    assert r.status == "blocked" and "USE" in (r.error_message or "")
    assert r.sql_sha256 == sql_sha256("USE otherdb")
    journal = (tmp / "journal" / "r1.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(journal) == 1 and '"label": "F01-B001"' in journal[0]


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"sql": "SELECT 1", "batch_file": "b.sql"}, "exactly one"),
        ({}, "exactly one"),
        ({"sql": "SELECT 1", "run_id": "../x"}, "run_id"),
        ({"sql": "SELECT 1", "timeout_seconds": 99999}, "timeout"),
        ({"batch_file": "b.txt"}, ".sql"),
    ],
)
def test_tool_argument_guards(tool_env: Any, kwargs: dict[str, Any], reason: str) -> None:
    from mssql_mcp.tools.release import run_release_batch

    manager, targets, tmp = tool_env
    (tmp / "runs" / "b.txt").write_text("SELECT 1", encoding="utf-8")
    r = run_release_batch(manager, targets, "dev2", **kwargs)
    assert r.status == "blocked" and reason in (r.error_message or "")


def test_non_target_environment_blocked(tool_env: Any) -> None:
    from mssql_mcp.tools.release import run_release_batch

    manager, targets, _ = tool_env
    r = run_release_batch(manager, targets, "prod", sql="SELECT 1")
    assert r.status == "blocked" and "not a release target" in (r.error_message or "")
