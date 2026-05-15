"""Routing layer that lets a single MCP server pivot between named
environments (and the database within an environment) at runtime.

Why a manager instead of just one Database:

* Tool functions are registered against a stable callable (FastMCP keeps
  the function object). They can't rebind to a fresh ``Database``.
* Closures over ``manager.current_db`` always see the active connection
  pool, so switching is a single attribute change.
* The manager is the single place that closes the previous pool when
  the user switches — no leaks."""

from __future__ import annotations

import logging
from threading import Lock

from mssql_mcp.config import Settings
from mssql_mcp.db import Database
from mssql_mcp.environments import Environment, EnvironmentRegistry

_LOG = logging.getLogger(__name__)


class NoEnvironmentSelectedError(RuntimeError):
    """Raised when a tool is called before any environment is active."""


class DatabaseManager:
    """Holds the currently-active :class:`Database` and the registry of
    environments it can be swapped to.

    Two operating modes:

    * **Legacy / single-server** — ``registry`` is ``None``. Settings carry
      ``mssql_server`` + ``mssql_database`` and the manager builds one
      Database eagerly. ``switch_environment`` and friends are no-ops.
    * **Registry mode** — ``registry`` is provided. The manager builds
      a Database lazily when the first env is activated (either the
      registry's ``default`` or via ``switch_environment``)."""

    def __init__(
        self,
        settings: Settings,
        registry: EnvironmentRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self._db: Database | None = None
        self._env_name: str | None = None
        self._database_name: str | None = None
        self._lock = Lock()

        if registry is None:
            # Legacy: settings already carry server + database.
            self._db = Database(settings)
            self._database_name = settings.mssql_database
        elif registry.default is not None:
            self.switch_environment(registry.default)

    # ------------------------------------------------------------------
    # state queries

    @property
    def current_db(self) -> Database:
        if self._db is None:
            raise NoEnvironmentSelectedError(
                "No environment is active yet. Call `switch_environment` "
                "with one of the configured environment names first."
            )
        return self._db

    @property
    def current_env_name(self) -> str | None:
        return self._env_name

    @property
    def current_database_name(self) -> str | None:
        return self._database_name

    @property
    def current_environment(self) -> Environment | None:
        if self._env_name is None or self.registry is None:
            return None
        return self.registry.get(self._env_name)

    # ------------------------------------------------------------------
    # mutators

    def switch_environment(
        self, name: str, database: str | None = None
    ) -> None:
        """Activate environment ``name`` (and optionally a specific database).

        Closes the previous pool. Raises ``KeyError`` if ``name`` isn't
        in the registry."""
        if self.registry is None:
            raise RuntimeError(
                "No environments registry configured — server was started "
                "in single-server mode. Set MSSQL_ENVIRONMENTS_FILE."
            )
        env = self.registry.get(name)
        target_db = database or env.default_database
        if not target_db:
            raise ValueError(
                f"Environment {name!r} has no default_database; "
                "pass an explicit database name to switch_environment."
            )
        # Build a fresh Settings with this env's server/db spliced in.
        new_settings = self.settings.model_copy(
            update={
                "mssql_server": env.server,
                "mssql_port": env.port,
                "mssql_database": target_db,
            }
        )
        with self._lock:
            old = self._db
            self._db = Database(new_settings)
            self._env_name = name
            self._database_name = target_db
            if old is not None:
                old.close_all()
        _LOG.info(
            "Active environment: %s (server=%s, database=%s)",
            name,
            env.server,
            target_db,
        )

    def switch_database(self, database: str) -> None:
        """Switch the active database within the current environment."""
        if self._env_name is None:
            raise NoEnvironmentSelectedError(
                "Pick an environment first, then a database."
            )
        self.switch_environment(self._env_name, database)


__all__ = ["DatabaseManager", "NoEnvironmentSelectedError"]
