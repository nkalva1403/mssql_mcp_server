"""Release-batch tool: run one reviewed ``GO`` batch on an allow-listed
environment. All locking lives in :mod:`mssql_mcp.release`; this layer
resolves the target and the batch text, applies the guard, runs, audits,
and (with ``run_id``) appends the full result to the run's journal."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from mssql_mcp import audit
from mssql_mcp.environments import Environment
from mssql_mcp.manager import DatabaseManager
from mssql_mcp.models import ReleaseBatchResult
from mssql_mcp.release import (
    MAX_RELEASE_TIMEOUT_SECONDS,
    ReleaseGuardError,
    check_batch,
    run_batch,
    sql_sha256,
)

MAX_BATCH_FILE_BYTES = 5 * 1024 * 1024
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


def load_batch_file(root: Path | None, batch_file: str) -> str:
    """Read a ``.sql`` batch file that must live under ``root``."""
    if root is None:
        raise ReleaseGuardError("batch_file needs MSSQL_RELEASE_BATCH_ROOT to be configured")
    path = Path(batch_file)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not path.is_relative_to(root.resolve()):
        raise ReleaseGuardError(f"batch_file must be under the release batch root ({root})")
    if path.suffix.lower() != ".sql":
        raise ReleaseGuardError("batch_file must be a .sql file")
    if not path.is_file():
        raise ReleaseGuardError(f"batch_file not found: {path}")
    if path.stat().st_size > MAX_BATCH_FILE_BYTES:
        raise ReleaseGuardError("batch_file is larger than 5 MB")
    return path.read_text(encoding="utf-8-sig")


def journal_dir(manager: DatabaseManager) -> Path:
    s = manager.settings
    return s.mssql_release_journal_dir or s.mssql_audit_log_path.parent / "release-journal"


def _journal(manager: DatabaseManager, result: ReleaseBatchResult) -> None:
    if not result.run_id or not _RUN_ID_RE.match(result.run_id):
        return
    folder = journal_dir(manager)
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / f"{result.run_id}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(result.model_dump(), ensure_ascii=False) + "\n")


def run_release_batch(
    manager: DatabaseManager,
    targets: dict[str, Environment],
    environment: str,
    sql: str | None = None,
    *,
    batch_file: str | None = None,
    label: str | None = None,
    session_settings: list[str] | None = None,
    timeout_seconds: int | None = None,
    run_id: str | None = None,
) -> ReleaseBatchResult:
    """Guard, execute, audit and journal one release batch."""
    settings_list = list(session_settings or [])
    target = targets.get(environment)
    database = target.default_database if target and target.default_database else ""
    timeout = timeout_seconds or manager.settings.mssql_release_timeout
    text = sql or ""
    try:
        if run_id is not None and not _RUN_ID_RE.match(run_id):
            raise ReleaseGuardError("run_id may only contain letters, digits, '.', '_' and '-'")
        if (sql is None) == (batch_file is None):
            raise ReleaseGuardError("pass exactly one of sql or batch_file")
        if batch_file is not None:
            text = load_batch_file(manager.settings.mssql_release_batch_root, batch_file)
        if target is None:
            raise ReleaseGuardError(
                f"environment {environment!r} is not a release target; "
                f"allowed: {sorted(targets)}"
            )
        if not 0 < timeout <= MAX_RELEASE_TIMEOUT_SECONDS:
            raise ReleaseGuardError(
                f"timeout_seconds must be between 1 and {MAX_RELEASE_TIMEOUT_SECONDS}"
            )
        check_batch(
            text, target=target, registry=manager.registry, session_settings=settings_list
        )
    except ReleaseGuardError as exc:
        audit.log_statement(
            tool="run_release_batch",
            sql=text or (batch_file or ""),
            params=None,
            duration_ms=0,
            success=False,
            error=exc,
            level=logging.WARNING,
        )
        blocked = ReleaseBatchResult(
            status="blocked",
            environment=environment,
            database=database,
            label=label,
            run_id=run_id,
            batch_file=batch_file,
            started_at="",
            duration_ms=0,
            sql_sha256=sql_sha256(text),
            error_message=str(exc),
        )
        _journal(manager, blocked)
        return blocked

    db = manager.db_for(environment)  # always the env's default database
    outcome = run_batch(db, text, session_settings=settings_list, timeout_seconds=timeout)
    audit.log_statement(
        tool="run_release_batch",
        sql=text,
        params=None,
        duration_ms=outcome.duration_ms,
        rows_affected=sum(outcome.rows_affected),
        success=outcome.status == "succeeded",
        error=outcome.error_message,
        level=logging.WARNING,
    )
    result = ReleaseBatchResult(
        status=outcome.status,  # type: ignore[arg-type]
        environment=environment,
        database=database,
        label=label,
        run_id=run_id,
        batch_file=batch_file,
        started_at=outcome.started_at,
        duration_ms=outcome.duration_ms,
        sql_sha256=outcome.sql_sha256,
        rows_affected=outcome.rows_affected,
        result_sets_returned=outcome.result_sets_returned,
        messages=outcome.messages,
        error_number=outcome.error_number,
        error_severity=outcome.error_severity,
        error_state=outcome.error_state,
        error_line=outcome.error_line,
        error_procedure=outcome.error_procedure,
        error_message=outcome.error_message,
        transaction_rolled_back=outcome.transaction_rolled_back,
    )
    _journal(manager, result)
    return result
