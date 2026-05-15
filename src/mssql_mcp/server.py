"""FastMCP server wiring.

Builds a :class:`FastMCP` instance with tools registered conditionally
based on :attr:`Settings.mssql_read_only` and :attr:`Settings.mssql_allow_ddl`,
configures the audit logger, and logs the resolved auth + environment
mode at startup so operators can see at a glance which connection flow
is in use.

When :attr:`Settings.mssql_environments_file` is set, the server runs
in *registry mode* — multiple named environments are available and tools
operate against whichever one is currently active. Otherwise it runs in
single-server mode for backward compatibility."""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from mssql_mcp import audit
from mssql_mcp.config import Settings, get_settings
from mssql_mcp.environments import EnvironmentRegistry
from mssql_mcp.manager import DatabaseManager
from mssql_mcp.models import (
    CurrentEnvironment,
    DdlResult,
    EnvironmentInfo,
    ForeignKeyInfo,
    IndexInfo,
    NonQueryResult,
    QueryParam,
    QueryResult,
    ServerInfo,
    TableDescription,
    TableInfo,
)
from mssql_mcp.tools import diagnostics as diagnostics_tools
from mssql_mcp.tools import environments as env_tools
from mssql_mcp.tools import indexes as indexes_tools
from mssql_mcp.tools import query as query_tools
from mssql_mcp.tools import schema as schema_tools

_LOG = logging.getLogger(__name__)


def _last4(value: str | None) -> str:
    if not value:
        return "<unset>"
    return "..." + value[-4:]


def build_server(settings: Settings) -> tuple[FastMCP, DatabaseManager]:
    """Construct a FastMCP server bound to a :class:`DatabaseManager`."""
    audit.configure(settings.mssql_audit_log_path)

    registry: EnvironmentRegistry | None = None
    if settings.mssql_environments_file is not None:
        registry = EnvironmentRegistry.from_path(settings.mssql_environments_file)
    manager = DatabaseManager(settings, registry=registry)

    mcp = FastMCP(
        "mssql-mcp-server",
        instructions=(
            "Query a Microsoft SQL Server database. Always prefer "
            "parameterised queries via the `params` argument over "
            "string-interpolated values. If multiple environments are "
            "configured, call `list_environments` and `current_environment` "
            "before assuming which server you're talking to; use "
            "`switch_environment` / `switch_database` to move."
        ),
        host=settings.mcp_http_host,
        port=settings.mcp_http_port,
        log_level=settings.log_level.upper(),  # type: ignore[arg-type]
    )

    # ----- environment management (always on when registry mode) -----------

    @mcp.tool()
    def list_environments() -> list[EnvironmentInfo]:
        """List every configured SQL Server environment (name, server,
        port, default database). Use ``switch_environment`` to make one
        of them active."""
        return env_tools.list_environments(manager)

    @mcp.tool()
    def current_environment() -> CurrentEnvironment:
        """Return the environment + database the server is currently
        connected to. Always check this first if multiple environments
        are configured."""
        return env_tools.current_environment(manager)

    @mcp.tool()
    def switch_environment(
        name: str, database: str | None = None
    ) -> CurrentEnvironment:
        """Pivot to a different configured environment. ``name`` must be
        one of the names returned by ``list_environments``. Optionally
        also pin to a specific database in that environment; otherwise
        the environment's default database is used."""
        return env_tools.switch_environment(manager, name, database=database)

    @mcp.tool()
    def switch_database(database: str) -> CurrentEnvironment:
        """Switch the active database within the *current* environment.
        Use ``switch_environment`` first if you need a different server."""
        return env_tools.switch_database(manager, database)

    # ----- schema introspection (always on) --------------------------------

    @mcp.tool()
    def list_databases() -> list[str]:
        """List user databases on the connected server (excludes master,
        tempdb, model, msdb)."""
        return schema_tools.list_databases(manager.current_db)

    @mcp.tool()
    def list_schemas(database: str | None = None) -> list[str]:
        """List schemas in the current database, or in the named database
        if one is provided."""
        return schema_tools.list_schemas(manager.current_db, database=database)

    @mcp.tool()
    def list_tables(
        schema: str | None = None, name_pattern: str | None = None
    ) -> list[TableInfo]:
        """List tables and views. ``name_pattern`` is a SQL ``LIKE``
        pattern (``%`` wildcards). Row counts are estimates from
        ``sys.dm_db_partition_stats`` and may lag actual COUNT(*)."""
        return schema_tools.list_tables(
            manager.current_db, schema=schema, name_pattern=name_pattern
        )

    @mcp.tool()
    def describe_table(schema: str, table: str) -> TableDescription:
        """Return columns, primary key, and a ready-to-run sample SELECT
        for one table."""
        return schema_tools.describe_table(
            manager.current_db, schema=schema, table=table
        )

    @mcp.tool()
    def list_indexes(schema: str, table: str) -> list[IndexInfo]:
        """List non-heap indexes on a table — key columns, included
        columns, filter definitions, and (when the DMV is granted)
        fragmentation percentage."""
        return indexes_tools.list_indexes(
            manager.current_db, schema=schema, table=table
        )

    @mcp.tool()
    def list_foreign_keys(schema: str, table: str) -> list[ForeignKeyInfo]:
        """List foreign keys touching a table in both directions
        (outgoing FKs declared on the table; incoming FKs declared on
        other tables that reference it)."""
        return indexes_tools.list_foreign_keys(
            manager.current_db, schema=schema, table=table
        )

    # ----- query execution -------------------------------------------------

    @mcp.tool()
    def execute_query(
        sql: str, params: list[QueryParam] | None = None
    ) -> QueryResult:
        """Run a read-only T-SQL statement (SELECT or CTE-led SELECT).

        Prefer parameterised queries: pass each value as a
        ``QueryParam`` and use ``?`` placeholders in the SQL. Inlining
        values into the SQL is allowed but discouraged.

        Results are capped at ``MAX_ROWS`` rows; when the cap is hit
        ``truncated`` is set to True."""
        return query_tools.execute_query(manager.current_db, sql, params)

    # ----- diagnostics -----------------------------------------------------

    @mcp.tool()
    def explain_query(sql: str) -> dict[str, str]:
        """Return the estimated execution plan for ``sql`` without
        running it."""
        plan = diagnostics_tools.explain_query(manager.current_db, sql)
        return {"plan_xml": plan.plan_xml, "summary": plan.summary}

    @mcp.tool()
    def server_info() -> ServerInfo:
        """Return the SQL Server version, current database / user
        (and ``ORIGINAL_LOGIN()``), the **active MCP mode** (read-only,
        write, or DDL), and the **active auth mode**."""
        return diagnostics_tools.server_info(manager.current_db)

    # ----- write tools, registered conditionally ---------------------------

    if not settings.mssql_read_only:

        @mcp.tool()
        def execute_non_query(
            sql: str, params: list[QueryParam] | None = None
        ) -> NonQueryResult:
            """Run an INSERT, UPDATE, DELETE, or MERGE.

            Wrapped in an explicit transaction. If the statement would
            affect more than the configured ``max_affected_rows``
            (default 10 000) the change is rolled back. Always use
            parameterised values."""
            return query_tools.execute_non_query(
                manager.current_db, sql, params
            )

    if not settings.mssql_read_only and settings.mssql_allow_ddl:

        @mcp.tool()
        def execute_ddl(sql: str) -> DdlResult:
            """Run a single CREATE / ALTER / DROP / TRUNCATE statement.

            Audited at WARNING level. Use this sparingly — schema
            changes from an LLM-driven workflow are inherently risky."""
            return query_tools.execute_ddl(manager.current_db, sql)

    # ----- startup logging -------------------------------------------------

    _LOG.info(
        "Auth mode: %s (tenant=%s, client=%s)",
        settings.mssql_auth_mode,
        _last4(settings.azure_tenant_id),
        _last4(settings.azure_client_id),
    )
    if registry is not None:
        _LOG.info(
            "Environments registry: %d entries (default=%s, active=%s)",
            len(registry.environments),
            registry.default,
            manager.current_env_name,
        )
    _LOG.info(
        "Mode: %s | Max rows: %d | Query timeout: %ds | Audit: %s",
        ("READ_ONLY" if settings.mssql_read_only else
         ("DDL" if settings.mssql_allow_ddl else "WRITE")),
        settings.mssql_max_rows,
        settings.mssql_query_timeout,
        settings.mssql_audit_log_path,
    )
    return mcp, manager


# Module-level singletons used by ``python -m mssql_mcp``.
_settings = get_settings()
mcp, _manager = build_server(_settings)
settings: Settings = _settings
