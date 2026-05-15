"""Tools for inspecting and switching between configured environments."""

from __future__ import annotations

from mssql_mcp.manager import DatabaseManager
from mssql_mcp.models import CurrentEnvironment, EnvironmentInfo


def list_environments(manager: DatabaseManager) -> list[EnvironmentInfo]:
    """Return every SQL Server environment this MCP server knows about."""
    if manager.registry is None:
        # Synthesise a single "default" entry from settings so the model
        # still gets a meaningful response in single-server mode.
        s = manager.settings
        if not s.mssql_server:
            return []
        return [
            EnvironmentInfo(
                name="default",
                description="(single-server mode, no environments file configured)",
                server=s.mssql_server,
                port=s.mssql_port,
                default_database=s.mssql_database,
            )
        ]
    return [
        EnvironmentInfo(
            name=env.name,
            description=env.description,
            server=env.server,
            port=env.port,
            default_database=env.default_database,
        )
        for env in manager.registry.environments
    ]


def current_environment(manager: DatabaseManager) -> CurrentEnvironment:
    """Return the environment + database the server is currently connected to."""
    env = manager.current_environment
    name = manager.current_env_name or "default"
    database = manager.current_database_name or ""
    if env is not None:
        return CurrentEnvironment(
            name=name,
            description=env.description,
            server=env.server,
            port=env.port,
            database=database,
        )
    # Single-server mode
    s = manager.settings
    return CurrentEnvironment(
        name=name,
        description="(single-server mode)",
        server=s.mssql_server or "",
        port=s.mssql_port,
        database=database,
    )


def switch_environment(
    manager: DatabaseManager,
    name: str,
    database: str | None = None,
) -> CurrentEnvironment:
    """Activate environment ``name`` (and optionally a specific database
    within it). Returns the new current_environment so the caller can
    confirm what got selected."""
    manager.switch_environment(name, database=database)
    return current_environment(manager)


def switch_database(
    manager: DatabaseManager, database: str
) -> CurrentEnvironment:
    """Switch to a different database inside the *current* environment.
    Use ``switch_environment`` first if you need a different server."""
    manager.switch_database(database)
    return current_environment(manager)
