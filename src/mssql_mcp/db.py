"""Database access layer.

Owns three concerns:

* The :func:`classify` statement parser — every SQL string passed to a
  tool flows through this gate before pyodbc ever sees it.
* :class:`Database` — a thin wrapper around pyodbc that manages a
  **token-aware** connection pool, applies the query timeout, enforces
  the row cap, wraps writes in a transaction, and transparently retries
  a single time if an Entra access token expires mid-query.
* Error sanitisation so server names / paths / stack traces never leak
  back to the model.

The classifier is intentionally pure (no I/O) and the part covered by
unit tests; the connection path is exercised by integration tests and by
mock-based unit tests."""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, cast

import pyodbc

from mssql_mcp import audit
from mssql_mcp.auth import (
    SQL_COPT_SS_ACCESS_TOKEN,
    TokenProvider,
    TokenStruct,
)
from mssql_mcp.config import Settings

# We manage our own pool to track per-connection token expiry. pyodbc's
# process-wide pool would happily hand back a connection whose access
# token has lapsed.
pyodbc.pooling = False


# --- Classifier ------------------------------------------------------------


class Mode(StrEnum):
    """The set of statement kinds the server is currently willing to run."""

    READ_ONLY = "read_only"
    WRITE = "write"
    DDL = "ddl"


ALLOWED_IN_READ_ONLY: frozenset[str] = frozenset({"SELECT", "WITH"})
ALLOWED_IN_WRITE: frozenset[str] = ALLOWED_IN_READ_ONLY | frozenset(
    {"INSERT", "UPDATE", "DELETE", "MERGE"}
)
ALLOWED_IN_DDL: frozenset[str] = ALLOWED_IN_WRITE | frozenset(
    {"CREATE", "ALTER", "DROP", "TRUNCATE"}
)
ALWAYS_BLOCKED: frozenset[str] = frozenset(
    {
        "EXEC",
        "EXECUTE",
        "GRANT",
        "REVOKE",
        "DENY",
        "BACKUP",
        "RESTORE",
        "SHUTDOWN",
        "RECONFIGURE",
        "KILL",
        "DBCC",
        "BULK",
        "OPENROWSET",
        "OPENDATASOURCE",
    }
)
DDL_KEYWORDS: frozenset[str] = frozenset({"CREATE", "ALTER", "DROP", "TRUNCATE"})

_ALLOWLIST_BY_MODE: dict[Mode, frozenset[str]] = {
    Mode.READ_ONLY: ALLOWED_IN_READ_ONLY,
    Mode.WRITE: ALLOWED_IN_WRITE,
    Mode.DDL: ALLOWED_IN_DDL,
}


class ClassifierError(ValueError):
    """Raised when a SQL string fails the statement-classification gate."""


class DatabaseError(RuntimeError):
    """Wraps pyodbc errors after the message has been sanitised."""


def _strip_comments(sql: str) -> str:
    """Remove ``--`` line comments and ``/* ... */`` block comments while
    keeping string literals and bracket identifiers intact."""
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
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                c = sql[i]
                out.append(c)
                if c == quote:
                    if i + 1 < n and sql[i + 1] == quote:
                        out.append(sql[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        elif ch == "[":
            out.append(ch)
            i += 1
            while i < n and sql[i] != "]":
                out.append(sql[i])
                i += 1
            if i < n:
                out.append(sql[i])
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _split_statements(sql: str) -> list[str]:
    """Split on ``;`` while honouring string literals and bracket identifiers."""
    parts: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            while i < n:
                c = sql[i]
                buf.append(c)
                if c == quote:
                    if i + 1 < n and sql[i + 1] == quote:
                        buf.append(sql[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        elif ch == "[":
            buf.append(ch)
            i += 1
            while i < n and sql[i] != "]":
                buf.append(sql[i])
                i += 1
            if i < n:
                buf.append(sql[i])
                i += 1
        elif ch == ";":
            parts.append("".join(buf))
            buf = []
            i += 1
        else:
            buf.append(ch)
            i += 1
    parts.append("".join(buf))
    return [p for p in (s.strip() for s in parts) if p]


_WORD_RE = re.compile(r"\b\w+\b")


def classify(sql: str, *, mode: Mode) -> str:
    """Validate ``sql`` against the active allow-list and return the
    upper-cased leading keyword (e.g. ``"SELECT"``)."""
    if not sql or not sql.strip():
        raise ClassifierError("SQL statement is empty")

    cleaned = _strip_comments(sql)
    statements = _split_statements(cleaned)
    if not statements:
        raise ClassifierError("SQL statement is empty after stripping comments")
    if len(statements) > 1:
        raise ClassifierError(
            "Multiple SQL statements are not allowed; "
            "submit one statement per call"
        )

    stmt = statements[0]
    upper = stmt.upper()
    tokens = _WORD_RE.findall(upper)
    if not tokens:
        raise ClassifierError("SQL contains no executable keyword")

    for blocked in ALWAYS_BLOCKED:
        if blocked in tokens:
            raise ClassifierError(
                f"Statement keyword '{blocked}' is blocked for security"
            )

    head = cast(str, tokens[0])
    allowlist = _ALLOWLIST_BY_MODE[mode]
    if head not in allowlist:
        raise ClassifierError(
            f"Statement '{head}' is not allowed in {mode.value} mode"
        )
    return head


# --- Result dataclasses ----------------------------------------------------


@dataclass(frozen=True)
class FetchResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    truncated: bool
    duration_ms: int


@dataclass(frozen=True)
class ExecuteResult:
    rows_affected: int
    duration_ms: int


# --- Error sanitisation ----------------------------------------------------


_PATH_RE = re.compile(r"[A-Za-z]:\\[\w\\.\-]+")
_POSIX_PATH_RE = re.compile(r"/(?:[\w.\-]+/)+[\w.\-]+")
_TOKEN_EXPIRED_PATTERNS = (
    re.compile(r"token.*expired", re.IGNORECASE),
    re.compile(r"token-identified principal", re.IGNORECASE),
    re.compile(r"\bSSPI\b.*\btoken\b", re.IGNORECASE),
)


def sanitise_db_error(error: BaseException) -> str:
    """Strip Windows / POSIX paths and verbose server info from a pyodbc error."""
    text = str(error)
    text = _PATH_RE.sub("<path>", text)
    text = _POSIX_PATH_RE.sub("<path>", text)
    text = re.sub(r"\[SQL Server\][^\]]*\]", "[SQL Server]", text)
    return text[:500]


def is_token_expired_error(error: BaseException) -> bool:
    """Heuristic: does this pyodbc error look like an expired Entra token?"""
    text = str(error)
    return any(pat.search(text) for pat in _TOKEN_EXPIRED_PATTERNS)


# --- Pooled connection -----------------------------------------------------


@dataclass
class _PooledConnection:
    """A pyodbc connection plus its access-token expiry (for Entra modes)."""

    conn: Any
    expires_on: datetime | None = None
    in_use: bool = field(default=False)


# --- Database wrapper ------------------------------------------------------


class Database:
    """High-level access object.

    Hands out cursors with the right timeout, enforces the row cap, and
    classifies every statement. For Entra modes, manages a token-aware
    pool: each pooled connection is tagged with its access-token expiry,
    is evicted before that expiry, and a mid-query expiry triggers
    exactly one transparent retry."""

    def __init__(
        self,
        settings: Settings,
        token_provider: TokenProvider | None = None,
    ) -> None:
        self.settings = settings
        # Make sure the audit logger has a handler attached even when the
        # Database is used outside of server.build_server() — otherwise
        # token_acquired events from the TokenProvider go nowhere.
        audit.configure(settings.mssql_audit_log_path)
        if settings.is_entra_auth and token_provider is None:
            token_provider = TokenProvider(settings)
        self._token_provider = token_provider
        self._connection_string = settings.connection_string()
        self._pool: deque[_PooledConnection] = deque()
        self._pool_lock = threading.Lock()

    # ------------------------------------------------------------------
    # mode helpers

    @property
    def mode(self) -> Mode:
        if self.settings.mssql_read_only:
            return Mode.READ_ONLY
        if self.settings.mssql_allow_ddl:
            return Mode.DDL
        return Mode.WRITE

    # ------------------------------------------------------------------
    # connection construction & pooling

    def _open_raw(self) -> _PooledConnection:
        """Open a fresh ODBC connection, attaching an access token if Entra.

        pyodbc 5.x removed ``cursor.timeout``; the query timeout is set
        on the connection (``conn.timeout``) and applies to every cursor
        opened from it. The ``timeout=`` kwarg to ``connect()`` is the
        login timeout, which is separate and matters during handshake."""
        if self.settings.is_entra_auth:
            assert self._token_provider is not None  # narrowed by is_entra_auth
            token: TokenStruct = self._token_provider.get_token()
            conn = pyodbc.connect(
                self._connection_string,
                attrs_before={SQL_COPT_SS_ACCESS_TOKEN: token.bytes},
                timeout=self.settings.mssql_query_timeout,
            )
        else:
            conn = pyodbc.connect(
                self._connection_string,
                timeout=self.settings.mssql_query_timeout,
            )
        conn.timeout = self.settings.mssql_query_timeout
        return _PooledConnection(
            conn=conn,
            expires_on=token.expires_on if self.settings.is_entra_auth else None,
        )

    def _fresh_enough(self, pooled: _PooledConnection) -> bool:
        """True if the pooled connection's token isn't about to expire."""
        if pooled.expires_on is None:
            return True  # SQL / Windows auth — no expiry
        return not TokenProvider.needs_refresh(pooled.expires_on)

    def _acquire(self) -> _PooledConnection:
        """Pop a usable connection from the pool, or open a fresh one."""
        with self._pool_lock:
            while self._pool:
                candidate = self._pool.popleft()
                if self._fresh_enough(candidate):
                    candidate.in_use = True
                    return candidate
                # Token close to expiry — discard.
                self._close_quietly(candidate)
        pooled = self._open_raw()
        pooled.in_use = True
        return pooled

    def _release(
        self, pooled: _PooledConnection, *, broken: bool = False
    ) -> None:
        """Return ``pooled`` to the pool — or close it if it's bad."""
        pooled.in_use = False
        if broken or not self._fresh_enough(pooled):
            self._close_quietly(pooled)
            return
        with self._pool_lock:
            if len(self._pool) < self.settings.mssql_pool_size:
                self._pool.append(pooled)
            else:
                self._close_quietly(pooled)

    @staticmethod
    def _close_quietly(pooled: _PooledConnection) -> None:
        with suppress(Exception):
            pooled.conn.close()

    def close_all(self) -> None:
        """Close every pooled connection — for graceful shutdown / tests."""
        with self._pool_lock:
            while self._pool:
                self._close_quietly(self._pool.popleft())

    @contextmanager
    def _cursor(self) -> Iterator[Any]:
        """Yield a cursor bound to a pooled connection; handle release.

        Query timeout is already set on the connection in :meth:`_open_raw`,
        so every cursor opened here inherits it."""
        pooled = self._acquire()
        broken = False
        try:
            cur = pooled.conn.cursor()
            try:
                yield cur
            finally:
                with suppress(Exception):
                    cur.close()
        except pyodbc.Error:
            broken = True
            raise
        finally:
            self._release(pooled, broken=broken)

    # ------------------------------------------------------------------
    # internal one-retry executor

    def _execute_with_retry(
        self,
        operation: str,
        action: Any,
    ) -> Any:
        """Run ``action()`` on a fresh cursor; on a token-expired error
        from pyodbc, close the connection, refresh, and retry exactly once.

        ``action`` receives the cursor and returns whatever the caller
        wants — we don't interpret it.
        """
        attempts = 0
        last_error: BaseException | None = None
        while attempts < 2:
            attempts += 1
            pooled = self._acquire()
            broken = False
            try:
                cur = pooled.conn.cursor()
                try:
                    return action(cur, pooled)
                finally:
                    with suppress(Exception):
                        cur.close()
            except pyodbc.Error as exc:
                broken = True
                last_error = exc
                if (
                    attempts == 1
                    and self.settings.is_entra_auth
                    and is_token_expired_error(exc)
                ):
                    # Drop this connection, try once more with a fresh token.
                    continue
                raise DatabaseError(sanitise_db_error(exc)) from exc
            finally:
                self._release(pooled, broken=broken)
        # Should be unreachable, but guard for static checkers.
        raise DatabaseError(  # pragma: no cover
            sanitise_db_error(last_error)
            if last_error is not None
            else f"{operation} failed"
        )

    # ------------------------------------------------------------------
    # public helpers

    def fetch(
        self, sql: str, params: Sequence[Any] = ()
    ) -> FetchResult:
        """Run a read-only statement and return up to ``MSSQL_MAX_ROWS`` rows."""
        classify(sql, mode=Mode.READ_ONLY)
        cap = self.settings.mssql_max_rows
        start = time.perf_counter()

        def _do(cur: Any, _pooled: _PooledConnection) -> FetchResult:
            cur.execute(sql, params)
            fetched = cur.fetchmany(cap + 1)
            truncated = len(fetched) > cap
            rows = fetched[:cap]
            columns = (
                [d[0] for d in cur.description] if cur.description else []
            )
            data = [
                dict(zip(columns, row, strict=False)) for row in rows
            ]
            duration_ms = int((time.perf_counter() - start) * 1000)
            return FetchResult(
                columns=columns,
                rows=data,
                truncated=truncated,
                duration_ms=duration_ms,
            )

        return cast(FetchResult, self._execute_with_retry("fetch", _do))

    def execute(
        self, sql: str, params: Sequence[Any] = ()
    ) -> ExecuteResult:
        """Run an INSERT/UPDATE/DELETE/MERGE inside an explicit transaction."""
        if self.mode == Mode.READ_ONLY:
            raise DatabaseError("Server is read-only; writes are disabled")
        head = classify(sql, mode=Mode.WRITE)
        if head in DDL_KEYWORDS:
            raise DatabaseError(
                "DDL statements must be sent through execute_ddl"
            )

        start = time.perf_counter()

        def _do(cur: Any, pooled: _PooledConnection) -> ExecuteResult:
            cur.execute(sql, params)
            affected = int(cur.rowcount)
            if affected > self.settings.mssql_max_affected_rows:
                with suppress(pyodbc.Error):
                    pooled.conn.rollback()
                raise DatabaseError(
                    f"Statement would affect {affected} rows, "
                    f"which exceeds the limit of "
                    f"{self.settings.mssql_max_affected_rows}"
                )
            pooled.conn.commit()
            duration_ms = int((time.perf_counter() - start) * 1000)
            return ExecuteResult(
                rows_affected=affected, duration_ms=duration_ms
            )

        return cast(ExecuteResult, self._execute_with_retry("execute", _do))

    def execute_ddl(self, sql: str) -> ExecuteResult:
        """Run a single DDL statement (CREATE/ALTER/DROP/TRUNCATE)."""
        if self.mode != Mode.DDL:
            raise DatabaseError("DDL execution is not enabled on this server")
        head = classify(sql, mode=Mode.DDL)
        if head not in DDL_KEYWORDS:
            raise DatabaseError(
                "execute_ddl only accepts CREATE / ALTER / DROP / TRUNCATE"
            )

        start = time.perf_counter()

        def _do(cur: Any, pooled: _PooledConnection) -> ExecuteResult:
            cur.execute(sql)
            with suppress(pyodbc.Error):
                pooled.conn.commit()
            duration_ms = int((time.perf_counter() - start) * 1000)
            return ExecuteResult(rows_affected=0, duration_ms=duration_ms)

        return cast(ExecuteResult, self._execute_with_retry("execute_ddl", _do))


__all__ = [
    "ALLOWED_IN_DDL",
    "ALLOWED_IN_READ_ONLY",
    "ALLOWED_IN_WRITE",
    "ALWAYS_BLOCKED",
    "DDL_KEYWORDS",
    "ClassifierError",
    "Database",
    "DatabaseError",
    "ExecuteResult",
    "FetchResult",
    "Mode",
    "classify",
    "is_token_expired_error",
    "sanitise_db_error",
]
