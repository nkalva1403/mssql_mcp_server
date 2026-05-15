"""Query-execution tools.

Every tool here is intentionally *thin*: it asks :class:`Database` to do
the actual work, then translates the result into a Pydantic model and
emits one audit-log line."""

from __future__ import annotations

import logging

from mssql_mcp import audit
from mssql_mcp.db import (
    ClassifierError,
    Database,
    DatabaseError,
    Mode,
    classify,
)
from mssql_mcp.models import (
    CompactQueryResult,
    DdlResult,
    NonQueryResult,
    QueryParam,
    QueryResult,
    ResponseFormat,
    to_param_tuple,
)


def execute_query(
    db: Database,
    sql: str,
    params: list[QueryParam] | None = None,
    format: ResponseFormat = "dict",
) -> QueryResult | CompactQueryResult:
    """Execute a read-only T-SQL statement and return up to
    ``MSSQL_MAX_ROWS`` rows.

    ``format='compact'`` returns columnar rows (``list[list]``) instead of
    list-of-dicts — typically 40-60% smaller on wide / many-row results."""
    bound = to_param_tuple(params)
    try:
        result = db.fetch(sql, bound)
    except (ClassifierError, DatabaseError) as exc:
        audit.log_statement(
            tool="execute_query",
            sql=sql,
            params=bound,
            duration_ms=0,
            rows_returned=0,
            success=False,
            error=exc,
        )
        raise
    audit.log_statement(
        tool="execute_query",
        sql=sql,
        params=bound,
        duration_ms=result.duration_ms,
        rows_returned=len(result.rows),
        row_cap_hit=result.truncated,
        success=True,
    )
    if format == "compact":
        cols = result.columns
        compact_rows = [[row.get(c) for c in cols] for row in result.rows]
        return CompactQueryResult(
            columns=cols,
            rows=compact_rows,
            row_count=len(compact_rows),
            truncated=result.truncated,
            duration_ms=result.duration_ms,
        )
    return QueryResult(
        columns=result.columns,
        rows=result.rows,
        row_count=len(result.rows),
        truncated=result.truncated,
        duration_ms=result.duration_ms,
    )


def execute_non_query(
    db: Database,
    sql: str,
    params: list[QueryParam] | None = None,
) -> NonQueryResult:
    """Execute an INSERT, UPDATE, DELETE, or MERGE inside an explicit
    transaction. Rolls back if affected rows exceed the configured cap."""
    bound = to_param_tuple(params)
    try:
        result = db.execute(sql, bound)
    except (ClassifierError, DatabaseError) as exc:
        audit.log_statement(
            tool="execute_non_query",
            sql=sql,
            params=bound,
            duration_ms=0,
            rows_affected=0,
            success=False,
            error=exc,
        )
        raise
    audit.log_statement(
        tool="execute_non_query",
        sql=sql,
        params=bound,
        duration_ms=result.duration_ms,
        rows_affected=result.rows_affected,
        success=True,
    )
    return NonQueryResult(
        rows_affected=result.rows_affected,
        duration_ms=result.duration_ms,
    )


def execute_ddl(db: Database, sql: str) -> DdlResult:
    """Execute a single DDL statement (CREATE / ALTER / DROP / TRUNCATE)."""
    try:
        head = classify(sql, mode=Mode.DDL)
        result = db.execute_ddl(sql)
    except (ClassifierError, DatabaseError) as exc:
        audit.log_statement(
            tool="execute_ddl",
            sql=sql,
            params=None,
            duration_ms=0,
            success=False,
            error=exc,
            level=logging.WARNING,
        )
        raise
    audit.log_statement(
        tool="execute_ddl",
        sql=sql,
        params=None,
        duration_ms=result.duration_ms,
        success=True,
        level=logging.WARNING,
    )
    return DdlResult(statement=head, duration_ms=result.duration_ms)
