"""Routing layer that lets a single MCP server pivot between named
environments (and the database within an environment) at runtime, **and**
hold multiple environments live at once so cross-server comparison tools
can read from two servers without a context switch.

Why a manager instead of just one Database:

* Tool functions are registered against a stable callable (FastMCP keeps
  the function object). They can't rebind to a fresh ``Database``.
* Closures over ``manager.current_db`` always see the active connection
  pool, so switching is a single attribute change.
* The manager owns a *cache* of Database pools keyed by
  ``(env_name, database)``. Single-env tools keep using ``current_db``;
  cross-env tools call ``db_for(env_name, database=None)`` and get a
  cached or freshly-built pool. Nothing is closed on switch — close
  happens only at shutdown via :meth:`close_all`."""

from __future__ import annotations

import logging
from threading import Lock

from mssql_mcp.config import Settings
from mssql_mcp.db import Database
from mssql_mcp.environments import Environment, EnvironmentRegistry

_LOG = logging.getLogger(__name__)


class NoEnvironmentSelectedError(RuntimeError):
    """Raised when a tool is called before any environment is active."""


PoolKey = tuple[str, str]  # (env_name, database_name)


class DatabaseManager:
    """Holds the currently-active :class:`Database` and a cache of pools
    for every environment that's been touched in this process.

    Two operating modes:

    * **Legacy / single-server** — ``registry`` is ``None``. Settings carry
      ``mssql_server`` + ``mssql_database`` and the manager builds one
      Database eagerly. ``switch_environment`` and ``db_for`` are no-ops
      / not meaningful.
    * **Registry mode** — ``registry`` is provided. The manager builds
      a Database lazily when the first env is activated (either the
      registry's ``default`` or via ``switch_environment``). Subsequent
      ``db_for`` calls cache one Database per ``(env_name, database)``
      tuple so compare tools can stream from two servers concurrently."""

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
        self._pools: dict[PoolKey, Database] = {}
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
    # multi-env access

    def db_for(
        self, env_name: str, database: str | None = None
    ) -> Database:
        """Return a Database for ``env_name`` (and optionally a specific
        ``database`` within it), building and caching one if needed.

        Used by cross-environment tools (e.g. ``compare_procedure``) so
        two servers can be queried in the same call without a switch."""
        if self.registry is None:
            raise RuntimeError(
                "db_for requires registry mode — set MSSQL_ENVIRONMENTS_FILE"
            )
        env = self.registry.get(env_name)
        target_db = database or env.default_database
        if not target_db:
            raise ValueError(
                f"Environment {env_name!r} has no default_database; "
                "pass an explicit database name."
            )
        key: PoolKey = (env_name, target_db)
        with self._lock:
            cached = self._pools.get(key)
            if cached is not None:
                return cached
            new_settings = self.settings.model_copy(
                update={
                    "mssql_server": env.server,
                    "mssql_port": env.port,
                    "mssql_database": target_db,
                }
            )
            db = Database(new_settings)
            self._pools[key] = db
            return db

    # ------------------------------------------------------------------
    # mutators

    def switch_environment(
        self, name: str, database: str | None = None
    ) -> None:
        """Activate environment ``name`` (and optionally a specific database).

        Cached pools for *other* environments are left alive — cross-env
        compare tools may still be using them. Raises ``KeyError`` if
        ``name`` isn't in the registry."""
        if self.registry is None:
            raise RuntimeError(
                "No environments registry configured — server was started "
                "in single-server mode. Set MSSQL_ENVIRONMENTS_FILE."
            )
        db = self.db_for(name, database=database)
        env = self.registry.get(name)
        target_db = database or env.default_database
        assert target_db is not None  # narrowed by db_for
        with self._lock:
            self._db = db
            self._env_name = name
            self._database_name = target_db
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

    # ------------------------------------------------------------------
    # cleanup

    def close_all(self) -> None:
        """Close every cached pool (and the legacy single-server pool)."""
        with self._lock:
            for db in self._pools.values():
                db.close_all()
            self._pools.clear()
            if self._db is not None and self.registry is None:
                # Legacy mode: _db is owned directly, not via _pools.
                self._db.close_all()


__all__ = ["DatabaseManager", "NoEnvironmentSelectedError", "PoolKey"]
