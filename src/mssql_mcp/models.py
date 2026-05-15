"""Pydantic models for every MCP tool's input and output.

The schemas here become part of the tool descriptions that the LLM sees,
so field descriptions and ``Literal`` choices matter — they're the
contract.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ParamValue = str | int | float | bool | None


class QueryParam(BaseModel):
    """A single ``?`` placeholder binding for parameterised queries."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        ...,
        description=(
            "Human-readable label for the parameter. Not used for binding "
            "(positional `?` placeholders are bound by order) — present "
            "only for logging and self-documentation."
        ),
    )
    value: ParamValue = Field(
        ..., description="Scalar value to bind. Must be a SQL-friendly scalar."
    )
    sql_type: str = Field(
        "auto",
        description=(
            "Optional SQL type hint, e.g. 'nvarchar', 'int', 'datetime2'. "
            "Use 'auto' (the default) to let pyodbc infer."
        ),
    )


# --- Query execution ------------------------------------------------------


class QueryResult(BaseModel):
    """Result of an :func:`execute_query` call."""

    model_config = ConfigDict(extra="forbid")

    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int = Field(
        ...,
        description="Number of rows actually returned (after the row cap).",
    )
    truncated: bool = Field(
        ...,
        description=(
            "True when the row cap was hit; the underlying result set "
            "contained more rows than were returned."
        ),
    )
    duration_ms: int


class NonQueryResult(BaseModel):
    """Result of an :func:`execute_non_query` call (INSERT/UPDATE/DELETE/MERGE)."""

    model_config = ConfigDict(extra="forbid")

    rows_affected: int
    duration_ms: int


class DdlResult(BaseModel):
    """Result of an :func:`execute_ddl` call."""

    model_config = ConfigDict(extra="forbid")

    statement: str = Field(
        ..., description="Leading keyword of the DDL that ran (e.g. CREATE)."
    )
    duration_ms: int


class ExplainResult(BaseModel):
    """Result of an :func:`explain_query` call."""

    model_config = ConfigDict(extra="forbid")

    plan_xml: str
    summary: str = Field(
        ...,
        description=(
            "A short, human-readable summary of the estimated plan: operators "
            "used and any seek vs. scan choices the optimiser made."
        ),
    )


# --- Schema introspection -------------------------------------------------


TableKind = Literal["BASE TABLE", "VIEW"]


class TableInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_name: Annotated[str, Field(alias="schema")]
    name: str
    type: TableKind
    row_count_estimate: int = Field(
        ...,
        description=(
            "Estimated row count from sys.dm_db_partition_stats; cheap to "
            "compute but can lag actual COUNT(*) significantly."
        ),
    )


class ColumnInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    data_type: str
    max_length: int | None = None
    precision: int | None = None
    scale: int | None = None
    is_nullable: bool
    is_identity: bool
    default: str | None = None


class TableDescription(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_name: Annotated[str, Field(alias="schema")]
    name: str
    columns: list[ColumnInfo]
    primary_key: list[str] = Field(
        default_factory=list,
        description="Column names that make up the primary key, in order.",
    )
    sample_query: str = Field(
        ...,
        description=(
            "A safe, ready-to-run SELECT TOP (10) suggestion for this table."
        ),
    )


IndexType = Literal[
    "HEAP", "CLUSTERED", "NONCLUSTERED", "UNIQUE", "XML", "SPATIAL", "OTHER"
]


class IndexInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: IndexType
    is_unique: bool
    is_primary_key: bool
    key_columns: list[str]
    included_columns: list[str] = Field(default_factory=list)
    filter_definition: str | None = None
    fragmentation_pct: float | None = Field(
        default=None,
        description=(
            "Fragmentation percentage from sys.dm_db_index_physical_stats "
            "(LIMITED mode). Null when the DMV is not available."
        ),
    )


ForeignKeyDirection = Literal["outgoing", "incoming"]


class ForeignKeyInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    direction: ForeignKeyDirection = Field(
        ...,
        description=(
            "'outgoing' = FK declared on this table referencing another; "
            "'incoming' = FK declared on another table referencing this one."
        ),
    )
    from_schema: str
    from_table: str
    from_columns: list[str]
    to_schema: str
    to_table: str
    to_columns: list[str]
    on_delete: str | None = None
    on_update: str | None = None


# --- Diagnostics ----------------------------------------------------------


ServerMode = Literal["read_only", "write", "ddl"]
AuthModeName = Literal[
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


class EnvironmentInfo(BaseModel):
    """One configured SQL Server target, as seen by the model."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    server: str
    port: int
    default_database: str | None = None


class CurrentEnvironment(BaseModel):
    """Which environment + database the server is talking to right now."""

    model_config = ConfigDict(extra="forbid")

    name: str
    server: str
    port: int
    database: str = Field(
        ...,
        description="The database currently active (may differ from the env's default).",
    )
    description: str | None = None


class ServerInfo(BaseModel):
    """Snapshot of the connected server and the active MCP mode."""

    model_config = ConfigDict(extra="forbid")

    version: str
    edition: str | None = None
    database: str
    user: str = Field(
        ...,
        description=(
            "Current session user as SQL Server sees it (``SUSER_SNAME()``). "
            "Under Entra ID this is the principal name (e.g. an app "
            "registration display name)."
        ),
    )
    original_login: str | None = Field(
        default=None,
        description=(
            "Login the session was opened as (``ORIGINAL_LOGIN()``). Differs "
            "from ``user`` after ``EXECUTE AS`` or impersonation."
        ),
    )
    server_collation: str | None = None
    mode: ServerMode = Field(
        ...,
        description=(
            "The current MCP server mode — tells the model exactly what "
            "categories of statements it's allowed to send."
        ),
    )
    auth_mode: AuthModeName = Field(
        ...,
        description=(
            "The Entra ID / SQL auth flow this server is using. Lets the "
            "model see whose permissions it's operating under."
        ),
    )
    max_rows: int
    query_timeout_seconds: int


# --- Helpers --------------------------------------------------------------


def to_param_tuple(params: list[QueryParam] | None) -> tuple[ParamValue, ...]:
    """Convert a list of :class:`QueryParam` to a positional ``?`` tuple."""
    if not params:
        return ()
    return tuple(p.value for p in params)
