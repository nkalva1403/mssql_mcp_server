"""Diagnostic tools: ``explain_query`` and ``server_info``."""

from __future__ import annotations

import re
import time
from contextlib import suppress
from typing import Any

from mssql_mcp import audit
from mssql_mcp.db import Database, Mode, classify
from mssql_mcp.models import (
    AuthModeName,
    ExplainResult,
    ServerInfo,
    ServerMode,
)

_OPERATOR_RE = re.compile(r'PhysicalOp="([^"]+)"')
_EST_ROWS_RE = re.compile(r'EstimateRows="([^"]+)"')


def _summarise_plan(plan_xml: str) -> str:
    """Build a short human summary from a SHOWPLAN_XML document."""
    operators = _OPERATOR_RE.findall(plan_xml)
    est_rows = _EST_ROWS_RE.findall(plan_xml)
    parts: list[str] = []
    if operators:
        unique_ops = sorted(set(operators))
        parts.append("operators: " + ", ".join(unique_ops))
    else:
        parts.append("no plan operators parsed")
    seeks = sum(1 for op in operators if "Seek" in op)
    scans = sum(1 for op in operators if "Scan" in op)
    if seeks or scans:
        parts.append(f"seeks={seeks}, scans={scans}")
    if est_rows:
        try:
            max_est = max(float(x) for x in est_rows)
            parts.append(f"max estimated rows={max_est:g}")
        except ValueError:  # pragma: no cover - defensive
            pass
    return "; ".join(parts)


def explain_query(db: Database, sql: str) -> ExplainResult:
    """Return the estimated execution plan for ``sql`` without running it."""
    classify(sql, mode=Mode.READ_ONLY)
    start = time.perf_counter()
    plan_xml = ""
    success = True
    error: BaseException | None = None
    try:
        with db._cursor() as cur:  # noqa: SLF001 - intentional cross-module
            cur.execute("SET SHOWPLAN_XML ON")
            try:
                cur.execute(sql)
                row = cur.fetchone()
                if row is not None and row[0] is not None:
                    plan_xml = str(row[0])
            finally:
                with suppress(Exception):
                    cur.execute("SET SHOWPLAN_XML OFF")
    except Exception as exc:  # noqa: BLE001 - audited and re-raised
        success = False
        error = exc
        raise
    finally:
        duration_ms = int((time.perf_counter() - start) * 1000)
        audit.log_statement(
            tool="explain_query",
            sql=sql,
            params=None,
            duration_ms=duration_ms,
            success=success,
            error=error,
        )
    summary = _summarise_plan(plan_xml) if plan_xml else "no plan returned"
    return ExplainResult(plan_xml=plan_xml, summary=summary)


_SERVER_INFO_SQL = """
SELECT
    CAST(@@VERSION AS NVARCHAR(MAX)) AS version,
    CAST(SERVERPROPERTY('Edition') AS NVARCHAR(256)) AS edition,
    DB_NAME() AS [database],
    SUSER_SNAME() AS [user],
    ORIGINAL_LOGIN() AS [original_login],
    CAST(SERVERPROPERTY('Collation') AS NVARCHAR(256)) AS server_collation
""".strip()


def _mode_label(db: Database) -> ServerMode:
    return db.mode.value


def server_info(db: Database) -> ServerInfo:
    """Return the connected server's version, edition, current
    database/user, ``ORIGINAL_LOGIN()``, and the **active MCP mode**
    plus **active auth mode** so the model knows what kinds of
    statements it's permitted to send and whose permissions it is using."""
    result = db.fetch(_SERVER_INFO_SQL)
    row: dict[str, Any] = result.rows[0] if result.rows else {}
    auth_mode: AuthModeName = db.settings.mssql_auth_mode
    version_raw = str(row.get("version") or "")
    version = version_raw.splitlines()[0].strip() if version_raw else ""
    return ServerInfo(
        version=version,
        edition=row.get("edition"),
        database=str(row.get("database") or db.settings.mssql_database),
        user=str(row.get("user") or ""),
        original_login=row.get("original_login"),
        server_collation=row.get("server_collation"),
        mode=_mode_label(db),
        auth_mode=auth_mode,
        max_rows=db.settings.mssql_max_rows,
        query_timeout_seconds=db.settings.mssql_query_timeout,
    )
