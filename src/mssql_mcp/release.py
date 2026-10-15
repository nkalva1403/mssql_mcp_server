"""Release-batch execution: run one ``GO``-separated batch of a reviewed
release script against an allow-listed environment.

This is a deliberately separate gate from ``MSSQL_READ_ONLY`` /
``MSSQL_ALLOW_DDL``. Release scripts are not single statements — they
open with ``IF NOT EXISTS``, ``DECLARE``, ``BEGIN TRAN``, ``EXEC
sp_rename`` and so on — so the statement classifier in :mod:`db` cannot
judge them. Instead this module locks *where* a batch may run:

* only environments named in ``MSSQL_RELEASE_ENVIRONMENTS``;
* never an environment whose name or database looks like prod/staging
  (refused at startup, in code, regardless of configuration);
* only that environment's default database — no ``USE``, no three-part
  name pointing at another registered environment's database, no
  linked-server / ``OPENQUERY`` hops;
* never server-level operations (``BACKUP``, ``RESTORE``, logins,
  ``ALTER DATABASE``, ``xp_cmdshell`` ...).

Each batch runs on its own fresh, autocommit connection inside
``BEGIN TRY ... EXEC sys.sp_executesql ... END TRY BEGIN CATCH``, so the
batch behaves as it would as one SSMS batch, but the first error stops
it and is reported with its real number, severity, state and line. A
transaction the batch leaves open is rolled back and reported. The
connection is closed afterwards, so no session state leaks between
batches."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pyodbc

from mssql_mcp.db import Database, sanitise_db_error
from mssql_mcp.environments import Environment, EnvironmentRegistry

# Environment names / databases that may never be release targets, however
# MSSQL_RELEASE_ENVIRONMENTS is set.
FORBIDDEN_TARGET_RE = re.compile(r"prod|stg|stag|live", re.IGNORECASE)

MAX_RELEASE_TIMEOUT_SECONDS = 3600
MAX_MESSAGES = 50
MAX_MESSAGE_CHARS = 500
MAX_SESSION_SETTINGS = 20

_GO_LINE_RE = re.compile(r"^[ \t]*GO(?:[ \t]+\d+)?[ \t]*;?[ \t]*$", re.IGNORECASE | re.MULTILINE)

_SESSION_SET_RE = re.compile(
    r"^SET\s+(ANSI_NULLS|QUOTED_IDENTIFIER|ANSI_PADDING|ANSI_WARNINGS|ARITHABORT|"
    r"CONCAT_NULL_YIELDS_NULL|NUMERIC_ROUNDABORT|NOCOUNT|XACT_ABORT|ANSI_NULL_DFLT_ON)"
    r"\s+(ON|OFF)\s*;?$",
    re.IGNORECASE,
)

# (pattern, human reason). Matched against the batch with comments, string
# literals and [bracketed] identifiers removed, so data such as
# N'BACKUP job' or a column named [Use] never trips them.
_BLOCKED_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p, re.IGNORECASE), reason)
    for p, reason in (
        (r"\bUSE\s+(?!HINT\b|PLAN\b)\w", "USE <database> would leave the locked database"),
        (r"\bBACKUP\b", "BACKUP is not allowed"),
        (r"\bRESTORE\b", "RESTORE is not allowed"),
        (r"\bSHUTDOWN\b", "SHUTDOWN is not allowed"),
        (r"\bKILL\b", "KILL is not allowed"),
        (r"\bRECONFIGURE\b", "RECONFIGURE is not allowed"),
        (r"\bxp_cmdshell\b", "xp_cmdshell is not allowed"),
        (r"\bsp_configure\b", "sp_configure is not allowed"),
        (r"\bsp_addlinkedserver\b", "linked-server setup is not allowed"),
        (r"\bOPENROWSET\b", "OPENROWSET is not allowed"),
        (r"\bOPENDATASOURCE\b", "OPENDATASOURCE is not allowed"),
        (r"\bOPENQUERY\b", "OPENQUERY is not allowed"),
        (r"\bSETUSER\b", "SETUSER is not allowed"),
        (r"\bEXECUTE\s+AS\s+LOGIN\b|\bEXEC\s+AS\s+LOGIN\b", "EXECUTE AS LOGIN is not allowed"),
        (
            r"\b(CREATE|ALTER|DROP)\s+(DATABASE|LOGIN|ENDPOINT|CREDENTIAL|AVAILABILITY\s+GROUP)\b",
            "server/database-level DDL is not allowed",
        ),
        (r"\b(CREATE|ALTER|DROP)\s+SERVER\s+(ROLE|AUDIT)\b", "server-level DDL is not allowed"),
        (r"\bALTER\s+SERVER\b", "ALTER SERVER is not allowed"),
        (r"\bDBCC\s+(?!CHECKIDENT\b)\w", "only DBCC CHECKIDENT is allowed"),
        # four-part name: server.database.schema.object
        (
            r"\b\w+\s*\.\s*\w+\s*\.\s*\w*\s*\.\s*\w+\b",
            "four-part (linked-server) names are not allowed",
        ),
    )
)

_THREE_PART_RE = re.compile(r"(?<![\w.])(\w+)\s*\.\s*(\w*)\s*\.\s*(\w+)(?![\w.])")


class ReleaseConfigError(ValueError):
    """Raised at startup when MSSQL_RELEASE_ENVIRONMENTS is unsafe or invalid."""


class ReleaseGuardError(ValueError):
    """Raised when a batch or a call is refused before touching the database."""


# --- configuration ---------------------------------------------------------


def parse_release_environments(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [name.strip() for name in raw.split(",") if name.strip()]


def resolve_release_targets(
    raw: str | None, registry: EnvironmentRegistry | None
) -> dict[str, Environment]:
    """Validate ``MSSQL_RELEASE_ENVIRONMENTS`` against the registry.

    Returns ``{name: Environment}``; empty when the feature is off. Raises
    :class:`ReleaseConfigError` for anything unsafe — the server must not
    start with a release target it cannot vouch for."""
    names = parse_release_environments(raw)
    if not names:
        return {}
    if registry is None:
        raise ReleaseConfigError(
            "MSSQL_RELEASE_ENVIRONMENTS requires MSSQL_ENVIRONMENTS_FILE (registry mode)"
        )
    targets: dict[str, Environment] = {}
    for name in names:
        try:
            env = registry.get(name)
        except KeyError as exc:
            raise ReleaseConfigError(str(exc)) from exc
        if not env.default_database:
            raise ReleaseConfigError(
                f"release environment {name!r} has no default_database to lock to"
            )
        for value in (env.name, env.default_database, env.description or ""):
            if FORBIDDEN_TARGET_RE.search(value):
                raise ReleaseConfigError(
                    f"release environment {name!r} looks like prod/staging "
                    f"({value!r}); release batches may never target it"
                )
        targets[name] = env
    return targets


# --- batch guard -----------------------------------------------------------


def strip_comments_literals_brackets(sql: str) -> str:
    """Return ``sql`` with comments removed and every string literal /
    bracketed identifier replaced by a neutral placeholder."""
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":
            while i < n and sql[i] not in ("\n", "\r"):
                i += 1
        elif ch == "/" and nxt == "*":
            depth = 1
            i += 2
            while i < n and depth:
                if sql[i] == "/" and i + 1 < n and sql[i + 1] == "*":
                    depth += 1
                    i += 2
                elif sql[i] == "*" and i + 1 < n and sql[i + 1] == "/":
                    depth -= 1
                    i += 2
                else:
                    i += 1
            out.append(" ")
        elif ch == "'":
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append("''")
        elif ch == "[":
            i += 1
            while i < n:
                if sql[i] == "]":
                    if i + 1 < n and sql[i + 1] == "]":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append("x")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _unbracket_names(sql: str) -> str:
    """Comments and literals removed, but ``[name]`` kept as ``name`` so
    three-part references can be read."""
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":
            while i < n and sql[i] not in ("\n", "\r"):
                i += 1
        elif ch == "/" and nxt == "*":
            i += 2
            while i < n - 1 and not (sql[i] == "*" and sql[i + 1] == "/"):
                i += 1
            i += 2
            out.append(" ")
        elif ch == "'":
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append("''")
        elif ch in "[]":
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def check_batch(
    sql: str,
    *,
    target: Environment,
    registry: EnvironmentRegistry | None,
    session_settings: Iterable[str] = (),
) -> None:
    """Raise :class:`ReleaseGuardError` if the batch may not run on ``target``."""
    if not sql or not sql.strip():
        raise ReleaseGuardError("batch is empty")
    if _GO_LINE_RE.search(sql):
        raise ReleaseGuardError(
            "batch contains a GO separator; split the script and send one batch per call"
        )
    settings = list(session_settings)
    if len(settings) > MAX_SESSION_SETTINGS:
        raise ReleaseGuardError(f"at most {MAX_SESSION_SETTINGS} session settings are allowed")
    for s in settings:
        if not _SESSION_SET_RE.match(s.strip()):
            raise ReleaseGuardError(
                f"session setting {s!r} is not an allowed 'SET <option> ON|OFF' statement"
            )

    cleaned = strip_comments_literals_brackets(sql)
    for pattern, reason in _BLOCKED_PATTERNS:
        if pattern.search(cleaned):
            raise ReleaseGuardError(reason)

    # Three-part names that point at another registered environment's database.
    if registry is not None:
        other_dbs = {
            (e.default_database or "").lower()
            for e in registry.environments
            if e.default_database and e.default_database != target.default_database
        }
        for match in _THREE_PART_RE.finditer(_unbracket_names(sql)):
            if match.group(1).lower() in other_dbs:
                raise ReleaseGuardError(
                    f"batch references database {match.group(1)!r}, which belongs to "
                    "another environment; only the locked database may be touched"
                )


# --- execution -------------------------------------------------------------


_SENTINEL = "__rb_status"


def build_wrapper(session_settings: Iterable[str]) -> str:
    """The outer batch that runs the release batch via sp_executesql.

    ``session_settings`` are validated ``SET <option> ON|OFF`` lines; they
    run at session level so they apply to the inner batch exactly as a
    preceding ``SET QUOTED_IDENTIFIER ON`` / ``GO`` would in SSMS."""
    sets = "\n".join(s.strip().rstrip(";") + ";" for s in session_settings)
    return f"""SET NOCOUNT ON;
DECLARE @__rb_sql nvarchar(max) = ?;
DECLARE @__rb_rolled_back bit = 0;
SET NOCOUNT OFF;
{sets}
BEGIN TRY
    EXEC sys.sp_executesql @__rb_sql;
    SET NOCOUNT ON;
    IF @@TRANCOUNT > 0
    BEGIN
        ROLLBACK TRANSACTION;
        SET @__rb_rolled_back = 1;
        SELECT 'failed' AS {_SENTINEL}, 266 AS error_number, 16 AS error_severity,
               1 AS error_state, NULL AS error_line, NULL AS error_procedure,
               N'The batch left a transaction open; it was rolled back.' AS error_message,
               @__rb_rolled_back AS rolled_back, DB_NAME() AS db_name;
    END
    ELSE
        SELECT 'succeeded' AS {_SENTINEL}, NULL AS error_number, NULL AS error_severity,
               NULL AS error_state, NULL AS error_line, NULL AS error_procedure,
               NULL AS error_message, @__rb_rolled_back AS rolled_back, DB_NAME() AS db_name;
END TRY
BEGIN CATCH
    SET NOCOUNT ON;
    IF XACT_STATE() <> 0 OR @@TRANCOUNT > 0
    BEGIN
        ROLLBACK TRANSACTION;
        SET @__rb_rolled_back = 1;
    END
    SELECT 'failed' AS {_SENTINEL}, ERROR_NUMBER() AS error_number,
           ERROR_SEVERITY() AS error_severity, ERROR_STATE() AS error_state,
           ERROR_LINE() AS error_line, ERROR_PROCEDURE() AS error_procedure,
           ERROR_MESSAGE() AS error_message, @__rb_rolled_back AS rolled_back,
           DB_NAME() AS db_name;
END CATCH;"""


@dataclass
class BatchOutcome:
    status: str  # succeeded | failed | blocked
    started_at: str
    duration_ms: int
    sql_sha256: str
    rows_affected: list[int] = field(default_factory=list)
    result_sets_returned: int = 0
    messages: list[str] = field(default_factory=list)
    error_number: int | None = None
    error_severity: int | None = None
    error_state: int | None = None
    error_line: int | None = None
    error_procedure: str | None = None
    error_message: str | None = None
    transaction_rolled_back: bool = False
    database_after: str | None = None


def sql_sha256(sql: str) -> str:
    """Hash of the batch with line endings and trailing whitespace
    normalised, so the same script saved on Windows or Unix hashes alike."""
    norm = "\n".join(line.rstrip() for line in sql.replace("\r\n", "\n").split("\n")).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


_ODBC_PREFIX_RE = re.compile(r"(\[[^\]]*\])+")
_ERR_NUM_RE = re.compile(r"\((\d+)\)\s*(\(SQL\w+\))?\s*$")


def _message_text(raw: Any) -> str:
    text = raw[1] if isinstance(raw, tuple) and len(raw) > 1 else str(raw)
    text = _ODBC_PREFIX_RE.sub("", str(text)).strip()
    return text[:MAX_MESSAGE_CHARS]


def drain_results(cur: Any, outcome: BatchOutcome) -> None:
    """Walk every result set / row count / message the wrapper produced."""
    while True:
        for msg in getattr(cur, "messages", None) or []:
            if len(outcome.messages) < MAX_MESSAGES:
                outcome.messages.append(_message_text(msg))
        if cur.description:
            columns = [d[0] for d in cur.description]
            if columns and columns[0] == _SENTINEL:
                row = dict(zip(columns, cur.fetchone(), strict=False))
                outcome.status = str(row[_SENTINEL])
                outcome.error_number = row.get("error_number")
                outcome.error_severity = row.get("error_severity")
                outcome.error_state = row.get("error_state")
                outcome.error_line = row.get("error_line")
                outcome.error_procedure = row.get("error_procedure")
                outcome.error_message = row.get("error_message")
                outcome.transaction_rolled_back = bool(row.get("rolled_back"))
                outcome.database_after = row.get("db_name")
            else:
                # The batch's own SELECT output: counted, never fetched —
                # fetching can fail on types pyodbc cannot convert
                # (sql_variant, geography ...) and the rows aren't reported.
                outcome.result_sets_returned += 1
        elif cur.rowcount is not None and cur.rowcount >= 0:
            outcome.rows_affected.append(int(cur.rowcount))
        if not cur.nextset():
            break


def run_batch(
    db: Database,
    sql: str,
    *,
    session_settings: Iterable[str] = (),
    timeout_seconds: int,
) -> BatchOutcome:
    """Execute one already-guarded batch on a fresh autocommit connection."""
    settings = list(session_settings)
    outcome = BatchOutcome(
        status="failed", started_at=_now(), duration_ms=0, sql_sha256=sql_sha256(sql)
    )
    start = time.perf_counter()
    pooled = db._open_raw()  # dedicated connection; never returned to the pool
    try:
        pooled.conn.autocommit = True
        pooled.conn.timeout = timeout_seconds
        cur = pooled.conn.cursor()
        try:
            cur.execute(build_wrapper(settings), (sql,))
            drain_results(cur, outcome)
        finally:
            with suppress(Exception):
                cur.close()
    except pyodbc.Error as exc:
        # Connection-killing errors (severity 20+), timeouts, login failures.
        text = sanitise_db_error(exc)
        outcome.status = "failed"
        outcome.error_message = _message_text(text)
        num = _ERR_NUM_RE.search(text)
        if num:
            outcome.error_number = int(num.group(1))
        if isinstance(exc, pyodbc.OperationalError) and "HYT00" in str(exc):
            outcome.error_message = (
                f"Timed out after {timeout_seconds}s; the batch was cancelled. "
                "Statements that completed before the timeout stay committed "
                "unless the batch wrapped them in a transaction."
            )
    finally:
        with suppress(Exception):
            pooled.conn.close()
        outcome.duration_ms = int((time.perf_counter() - start) * 1000)
    if outcome.status == "succeeded" and outcome.database_after not in (
        None,
        db.settings.mssql_database,
    ):
        outcome.status = "failed"
        outcome.error_message = (
            f"Database context changed to {outcome.database_after!r} during the batch"
        )
    return outcome


__all__ = [
    "FORBIDDEN_TARGET_RE",
    "MAX_RELEASE_TIMEOUT_SECONDS",
    "BatchOutcome",
    "ReleaseConfigError",
    "ReleaseGuardError",
    "build_wrapper",
    "check_batch",
    "drain_results",
    "parse_release_environments",
    "resolve_release_targets",
    "run_batch",
    "sql_sha256",
    "strip_comments_literals_brackets",
]
