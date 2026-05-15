"""Append-only JSONL audit logger.

One line per executed statement or auth event. Parameter *values* are
deliberately never written — they might contain PII — only the parameter
count. Access tokens are never written at any log level. The full SQL is
identified by its SHA-256 hash; the preview is truncated to 500 chars."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

from pythonjsonlogger.json import JsonFormatter

SQL_PREVIEW_MAX_CHARS = 500
_LOGGER_NAME = "mssql_mcp.audit"
_lock = threading.Lock()
_configured_path: Path | None = None


def _build_preview(sql: str) -> str:
    cleaned = " ".join(sql.split())
    if len(cleaned) <= SQL_PREVIEW_MAX_CHARS:
        return cleaned
    return cleaned[: SQL_PREVIEW_MAX_CHARS - 1] + "…"


def _hash_sql(sql: str) -> str:
    digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


_PATH_RE_WIN = re.compile(r"[A-Za-z]:\\[\w\\.\-]+")
_PATH_RE_POSIX = re.compile(r"/(?:[\w.\-]+/)+[\w.\-]+")
_SQL_SERVER_BRACKET_RE = re.compile(r"\[SQL Server\][^\]]*\]")


def _sanitise_error(error: BaseException | str | None) -> str | None:
    if error is None:
        return None
    text = str(error)
    if not text:
        return None
    text = _PATH_RE_WIN.sub("<path>", text)
    text = _PATH_RE_POSIX.sub("<path>", text)
    text = _SQL_SERVER_BRACKET_RE.sub("[SQL Server]", text)
    return text[:500]


def _secure_file_mode(log_path: Path) -> None:
    """Set POSIX 0600 on the log file; no-op on Windows where ACLs apply."""
    if sys.platform == "win32":
        return
    try:
        if log_path.exists():
            os.chmod(log_path, 0o600)
    except OSError:  # pragma: no cover - defensive
        pass


def configure(log_path: Path) -> logging.Logger:
    """Configure the audit logger to write to ``log_path``.

    Called once at startup. Subsequent calls with the same path are
    no-ops; a call with a different path reconfigures the handler.
    """
    global _configured_path
    with _lock:
        logger = logging.getLogger(_LOGGER_NAME)
        if _configured_path == log_path and logger.handlers:
            return logger

        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()

        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = TimedRotatingFileHandler(
            log_path,
            when="midnight",
            backupCount=14,
            encoding="utf-8",
            delay=True,
            utc=True,
        )
        formatter = JsonFormatter(
            "%(message)s",
            json_ensure_ascii=False,
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        _configured_path = log_path
        _secure_file_mode(log_path)
        return logger


def get_logger() -> logging.Logger:
    """Return the configured audit logger."""
    return logging.getLogger(_LOGGER_NAME)


def _isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def log_statement(
    *,
    tool: str,
    sql: str,
    params: Mapping[str, Any] | list[Any] | tuple[Any, ...] | None,
    duration_ms: int,
    rows_returned: int | None = None,
    rows_affected: int | None = None,
    row_cap_hit: bool = False,
    success: bool = True,
    error: BaseException | str | None = None,
    level: int = logging.INFO,
) -> None:
    """Emit one ``sql_executed`` audit record."""
    if params is None:
        params_count = 0
    elif isinstance(params, Mapping):
        params_count = len(params)
    else:
        params_count = len(params)

    record: dict[str, Any] = {
        "timestamp": _isoformat(datetime.now(UTC)),
        "event": "sql_executed",
        "tool": tool,
        "sql_preview": _build_preview(sql),
        "sql_hash": _hash_sql(sql),
        "params_count": params_count,
        "duration_ms": duration_ms,
        "rows_returned": rows_returned,
        "rows_affected": rows_affected,
        "row_cap_hit": row_cap_hit,
        "success": success,
        "error": _sanitise_error(error),
    }
    get_logger().log(level, record)
    if _configured_path is not None:
        _secure_file_mode(_configured_path)


def _client_id_suffix(client_id: str | None) -> str | None:
    if not client_id:
        return None
    return "..." + client_id[-4:]


def log_token_acquired(
    *,
    auth_mode: str,
    expires_on: datetime,
    client_id: str | None = None,
    tenant_id: str | None = None,
) -> None:
    """Emit one ``token_acquired`` audit record.

    The access token itself is **never** written. Only the auth mode,
    expiry, and the *last four characters* of any client / tenant ID
    are included — enough to correlate logs without leaking secrets.
    """
    record: dict[str, Any] = {
        "timestamp": _isoformat(datetime.now(UTC)),
        "event": "token_acquired",
        "auth_mode": auth_mode,
        "expires_on": _isoformat(expires_on),
        "client_id_suffix": _client_id_suffix(client_id),
        "tenant_id_suffix": _client_id_suffix(tenant_id),
    }
    get_logger().info(record)
    if _configured_path is not None:
        _secure_file_mode(_configured_path)


def log_token_refresh_failed(
    *,
    auth_mode: str,
    error: BaseException | str,
) -> None:
    """Emit one ``token_refresh_failed`` audit record."""
    record: dict[str, Any] = {
        "timestamp": _isoformat(datetime.now(UTC)),
        "event": "token_refresh_failed",
        "auth_mode": auth_mode,
        "error": _sanitise_error(error),
    }
    get_logger().warning(record)
