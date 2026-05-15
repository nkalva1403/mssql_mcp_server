"""Application configuration loaded from environment variables."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AuthMode = Literal[
    "sql",
    "windows",
    "entra_service_principal",
    "entra_service_principal_cert",
    "entra_managed_identity",
    "entra_cli",
    "entra_default",
    "entra_interactive",
    "entra_device_code",
]
Transport = Literal["stdio", "http"]
YesNo = Literal["yes", "no"]

ENTRA_AUTH_MODES: frozenset[str] = frozenset(
    {
        "entra_service_principal",
        "entra_service_principal_cert",
        "entra_managed_identity",
        "entra_cli",
        "entra_default",
        "entra_interactive",
        "entra_device_code",
    }
)


class Settings(BaseSettings):
    """All runtime configuration for the MSSQL MCP server.

    Values come from environment variables (and optionally a ``.env`` file).
    Field names are case-insensitive; e.g. ``MSSQL_SERVER`` maps to
    :attr:`mssql_server` and ``AZURE_TENANT_ID`` to :attr:`azure_tenant_id`.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Connection ---------------------------------------------------------
    # When MSSQL_ENVIRONMENTS_FILE is set, mssql_server / mssql_database
    # become optional — the active environment provides them. Otherwise
    # both are required.
    mssql_server: str | None = Field(
        default=None, description="Host or host\\instance"
    )
    mssql_port: int = 1433
    mssql_database: str | None = Field(
        default=None, description="Default database"
    )
    mssql_driver: str = "ODBC Driver 18 for SQL Server"
    mssql_encrypt: YesNo = "yes"
    mssql_trust_server_cert: YesNo = "no"

    # --- Environment registry (optional) -----------------------------------
    mssql_environments_file: Path | None = None

    # --- Auth mode ----------------------------------------------------------
    mssql_auth_mode: AuthMode = "entra_default"

    # --- SQL / Windows credentials (used only by 'sql' mode) ---------------
    mssql_username: str | None = None
    mssql_password: str | None = None

    # --- Entra ID credentials ----------------------------------------------
    azure_tenant_id: str | None = None
    azure_client_id: str | None = None
    azure_client_secret: str | None = None
    azure_client_certificate_path: Path | None = None
    azure_client_certificate_password: str | None = None
    mssql_token_cache_path: Path = Path(
        "~/.cache/mssql-mcp/token-cache.bin"
    )
    mssql_disable_token_cache: bool = False

    # --- Behaviour ----------------------------------------------------------
    mssql_read_only: bool = True
    mssql_allow_ddl: bool = False
    mssql_max_rows: int = Field(default=1000, gt=0)
    mssql_query_timeout: int = Field(default=30, gt=0)
    mssql_pool_size: int = Field(default=5, gt=0)
    mssql_max_affected_rows: int = Field(default=10_000, gt=0)

    # --- Logging / transport ------------------------------------------------
    mssql_audit_log_path: Path = Path("./logs/audit.jsonl")
    mcp_transport: Transport = "stdio"
    mcp_http_host: str = "127.0.0.1"
    mcp_http_port: int = Field(default=8000, gt=0, lt=65536)
    log_level: str = "INFO"

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: object) -> object:
        if isinstance(value, str):
            return value.upper()
        return value

    @field_validator(
        "mssql_token_cache_path",
        "mssql_audit_log_path",
        "azure_client_certificate_path",
        "mssql_environments_file",
        mode="before",
    )
    @classmethod
    def _expand_user(cls, value: object) -> object:
        if isinstance(value, str) and value:
            return Path(value).expanduser()
        return value

    @model_validator(mode="after")
    def _validate_server_and_db_present(self) -> Settings:
        """Either an environments file *or* explicit MSSQL_SERVER+DATABASE must be set."""
        if self.mssql_environments_file is None:
            missing: list[str] = []
            if not self.mssql_server:
                missing.append("MSSQL_SERVER")
            if not self.mssql_database:
                missing.append("MSSQL_DATABASE")
            if missing:
                raise ValueError(
                    "When MSSQL_ENVIRONMENTS_FILE is not set, "
                    + " and ".join(missing)
                    + " is required"
                )
        return self

    @model_validator(mode="after")
    def _validate_auth_mode_requirements(self) -> Settings:
        mode = self.mssql_auth_mode
        if mode == "sql":
            if not self.mssql_username or not self.mssql_password:
                raise ValueError(
                    "MSSQL_USERNAME and MSSQL_PASSWORD are required when "
                    "MSSQL_AUTH_MODE=sql"
                )
        elif mode == "entra_service_principal":
            missing = [
                name
                for name, value in (
                    ("AZURE_TENANT_ID", self.azure_tenant_id),
                    ("AZURE_CLIENT_ID", self.azure_client_id),
                    ("AZURE_CLIENT_SECRET", self.azure_client_secret),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    "entra_service_principal requires: " + ", ".join(missing)
                )
        elif mode == "entra_service_principal_cert":
            missing = [
                name
                for name, value in (
                    ("AZURE_TENANT_ID", self.azure_tenant_id),
                    ("AZURE_CLIENT_ID", self.azure_client_id),
                    (
                        "AZURE_CLIENT_CERTIFICATE_PATH",
                        self.azure_client_certificate_path,
                    ),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    "entra_service_principal_cert requires: "
                    + ", ".join(missing)
                )
            if (
                self.azure_client_certificate_path is not None
                and not self.azure_client_certificate_path.exists()
            ):
                raise ValueError(
                    "AZURE_CLIENT_CERTIFICATE_PATH does not exist: "
                    f"{self.azure_client_certificate_path}"
                )
        # entra_interactive / entra_device_code: AZURE_TENANT_ID is
        # optional. When unset, MSAL routes through the ``organizations``
        # endpoint, which lets any work / school account sign in and
        # picks their home tenant automatically.
        # entra_managed_identity / entra_cli / entra_default need no extras
        return self

    @model_validator(mode="after")
    def _validate_ddl_requires_writes(self) -> Settings:
        if self.mssql_allow_ddl and self.mssql_read_only:
            raise ValueError(
                "MSSQL_ALLOW_DDL=true requires MSSQL_READ_ONLY=false"
            )
        return self

    @property
    def is_entra_auth(self) -> bool:
        return self.mssql_auth_mode in ENTRA_AUTH_MODES

    def connection_string(self) -> str:
        """Build the ODBC connection string.

        For Entra modes the string contains **no** credentials and **no**
        ``Authentication=`` keyword — the access token is supplied via
        the ``SQL_COPT_SS_ACCESS_TOKEN`` connection attribute (see
        :mod:`mssql_mcp.auth`)."""
        if not self.mssql_server or not self.mssql_database:
            raise RuntimeError(
                "connection_string() requires mssql_server and "
                "mssql_database — pick an environment first"
            )
        base = (
            f"DRIVER={{{self.mssql_driver}}};"
            f"SERVER={self.mssql_server},{self.mssql_port};"
            f"DATABASE={self.mssql_database};"
            f"Encrypt={self.mssql_encrypt};"
            f"TrustServerCertificate={self.mssql_trust_server_cert};"
        )
        if self.mssql_auth_mode == "sql":
            return base + f"UID={self.mssql_username};PWD={self.mssql_password};"
        if self.mssql_auth_mode == "windows":
            return base + "Trusted_Connection=yes;"
        return base

    def safe_connection_string(self) -> str:
        """Connection string with any secret-bearing clauses redacted.

        Use this whenever the connection string might be logged, even at
        DEBUG. Entra connection strings have nothing to redact (no creds
        in them by design); SQL-auth strings have their ``PWD`` masked."""
        cs = self.connection_string()
        import re

        return re.sub(r"(PWD=)[^;]*", r"\1***", cs)


def get_settings() -> Settings:
    """Build a fresh :class:`Settings` instance from the environment."""
    return Settings()
