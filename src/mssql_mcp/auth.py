"""Entra ID token acquisition for SQL Server.

Translates one of the ``entra_*`` auth modes into an :class:`azure.identity`
credential, fetches an access token for the SQL Database resource, and
encodes it in the byte layout pyodbc needs for the ``attrs_before``
connection attribute.

We intentionally leave token refresh to ``azure-identity`` — it caches
silently, refreshes near expiry, and only re-prompts when all silent
paths fail. The MCP server itself just calls :meth:`TokenProvider.get_token`
each time it needs a fresh token; the library does the work."""

from __future__ import annotations

import struct
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from azure.core.credentials import AccessToken, TokenCredential
from azure.identity import (
    AzureCliCredential,
    CertificateCredential,
    ClientSecretCredential,
    DefaultAzureCredential,
    DeviceCodeCredential,
    InteractiveBrowserCredential,
    ManagedIdentityCredential,
    TokenCachePersistenceOptions,
)

from mssql_mcp import audit
from mssql_mcp.config import ENTRA_AUTH_MODES, Settings

# Documented Microsoft ODBC driver attribute for SQL Server Entra access tokens.
SQL_COPT_SS_ACCESS_TOKEN = 1256

# Fixed scope for the SQL Database resource.
SQL_SCOPE = "https://database.windows.net/.default"

# How far in advance of expiry we consider a pooled connection unusable
# and rebuild it with a fresh token.
TOKEN_REFRESH_BUFFER = timedelta(minutes=5)


@dataclass(frozen=True)
class TokenStruct:
    """An ODBC-encoded access token plus its expiry timestamp."""

    bytes: bytes
    expires_on: datetime


def encode_token_for_pyodbc(access_token: str) -> bytes:
    """Encode an access token into the SQL_COPT_SS_ACCESS_TOKEN layout.

    SQL Server's ODBC driver expects: 4-byte little-endian length prefix,
    then the token bytes in UTF-16-LE. Get this wrong and the server
    returns a cryptic ``Login failed for user
    '<token-identified principal>'``."""
    utf16 = access_token.encode("utf-16-le")
    return struct.pack("<i", len(utf16)) + utf16


def verify_token_cache_permissions(path: Path) -> None:
    """Refuse to load a POSIX cache file looser than ``0600``.

    No-op on Windows where DPAPI / NTFS ACLs control access instead."""
    if sys.platform == "win32":
        return
    if not path.exists():
        return
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise PermissionError(
            f"Token cache file {path} has overly permissive mode "
            f"{oct(mode)}; expected 0600 or stricter. Refusing to load."
        )


class TokenProvider:
    """Acquires Entra ID access tokens for SQL Server.

    One instance per server. Each :meth:`get_token` call returns a fresh
    token (or a cached one if still valid) — refresh is delegated to
    ``azure-identity``."""

    def __init__(
        self,
        settings: Settings,
        credential: TokenCredential | None = None,
    ) -> None:
        if not settings.is_entra_auth:
            raise ValueError(
                f"TokenProvider is for Entra auth modes only, not "
                f"{settings.mssql_auth_mode!r}"
            )
        self._settings = settings
        self._credential = credential or self._build_credential()

    # ------------------------------------------------------------------
    # credential construction

    def _token_cache_options(self) -> TokenCachePersistenceOptions | None:
        """Persistent token cache for the *interactive* flows only.

        Service principal and managed identity flows acquire tokens
        silently and cheaply — caching them on disk adds attack surface
        without buying much."""
        if self._settings.mssql_disable_token_cache:
            return None
        if self._settings.mssql_auth_mode not in (
            "entra_interactive",
            "entra_device_code",
        ):
            return None
        cache_path = self._settings.mssql_token_cache_path
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        verify_token_cache_permissions(cache_path)
        return TokenCachePersistenceOptions(
            name="mssql-mcp", allow_unencrypted_storage=False
        )

    def _build_credential(self) -> TokenCredential:
        mode = self._settings.mssql_auth_mode
        cache_opts = self._token_cache_options()
        if mode == "entra_service_principal":
            return ClientSecretCredential(
                tenant_id=str(self._settings.azure_tenant_id),
                client_id=str(self._settings.azure_client_id),
                client_secret=str(self._settings.azure_client_secret),
            )
        if mode == "entra_service_principal_cert":
            cert_path = self._settings.azure_client_certificate_path
            kwargs: dict[str, Any] = {
                "tenant_id": str(self._settings.azure_tenant_id),
                "client_id": str(self._settings.azure_client_id),
                "certificate_path": str(cert_path) if cert_path else "",
            }
            if self._settings.azure_client_certificate_password:
                kwargs["password"] = (
                    self._settings.azure_client_certificate_password
                )
            return CertificateCredential(**kwargs)
        if mode == "entra_managed_identity":
            mi_kwargs: dict[str, Any] = {}
            if self._settings.azure_client_id:
                mi_kwargs["client_id"] = self._settings.azure_client_id
            return ManagedIdentityCredential(**mi_kwargs)
        if mode == "entra_cli":
            return AzureCliCredential()
        if mode == "entra_default":
            return DefaultAzureCredential(
                exclude_interactive_browser_credential=False,
            )
        if mode == "entra_interactive":
            interactive_kwargs: dict[str, Any] = {
                "tenant_id": self._settings.azure_tenant_id,
            }
            if cache_opts is not None:
                interactive_kwargs["cache_persistence_options"] = cache_opts
            return InteractiveBrowserCredential(**interactive_kwargs)
        if mode == "entra_device_code":
            device_kwargs: dict[str, Any] = {
                "tenant_id": self._settings.azure_tenant_id,
            }
            if cache_opts is not None:
                device_kwargs["cache_persistence_options"] = cache_opts
            return DeviceCodeCredential(**device_kwargs)
        raise ValueError(  # pragma: no cover - guarded by is_entra_auth check
            f"{mode!r} is not a supported Entra auth mode"
        )

    # ------------------------------------------------------------------
    # public surface

    def get_token(self) -> TokenStruct:
        """Acquire an access token for SQL Database; audit on success."""
        access_token: AccessToken = self._credential.get_token(SQL_SCOPE)
        expiry = datetime.fromtimestamp(access_token.expires_on, tz=UTC)
        audit.log_token_acquired(
            auth_mode=self._settings.mssql_auth_mode,
            expires_on=expiry,
            client_id=self._settings.azure_client_id,
            tenant_id=self._settings.azure_tenant_id,
        )
        return TokenStruct(
            bytes=encode_token_for_pyodbc(access_token.token),
            expires_on=expiry,
        )

    @staticmethod
    def needs_refresh(
        expires_on: datetime, *, now: datetime | None = None
    ) -> bool:
        """True if the token will expire within :data:`TOKEN_REFRESH_BUFFER`."""
        current = now or datetime.now(UTC)
        return (expires_on - current) <= TOKEN_REFRESH_BUFFER


__all__ = [
    "ENTRA_AUTH_MODES",
    "SQL_COPT_SS_ACCESS_TOKEN",
    "SQL_SCOPE",
    "TOKEN_REFRESH_BUFFER",
    "TokenProvider",
    "TokenStruct",
    "encode_token_for_pyodbc",
    "verify_token_cache_permissions",
]
