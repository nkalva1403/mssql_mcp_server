"""Pydantic models for every MCP tool's input and output.

The schemas here become part of the tool descriptions that the LLM sees,
so field names and ``Literal`` choices matter — they're the contract.
Descriptions are intentionally terse: every byte ships in every tool
call's schema."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ParamValue = str | int | float | bool | None

ResponseFormat = Literal["dict", "compact"]
"""``dict`` = list-of-dicts rows (default, human-readable).
``compact`` = columnar (``columns`` + ``rows: list[list]``), 40-60%
smaller payload, preferred when the model is just reading data."""


class QueryParam(BaseModel):
    """A single ``?`` placeholder binding for parameterised queries."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Label for logs. Bindings are positional.")
    value: ParamValue = Field(..., description="Scalar value to bind.")
    sql_type: str = Field("auto", description="Type hint or 'auto'.")


# --- Query execution ------------------------------------------------------


class QueryResult(BaseModel):
    """Result of :func:`execute_query` with ``format='dict'`` (default).

    Each row is a ``{column: value}`` dict. Human-friendly; ~2x larger
    than compact mode on wide result sets."""

    model_config = ConfigDict(extra="forbid")

    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool = Field(..., description="True if the row cap was hit.")
    duration_ms: int
    format: Literal["dict"] = "dict"


class CompactQueryResult(BaseModel):
    """Result of :func:`execute_query` with ``format='compact'``.

    Rows are positional arrays aligned to ``columns``. Use when the model
    only needs to read the data — saves significant tokens on wide or
    many-row results."""

    model_config = ConfigDict(extra="forbid")

    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool = Field(..., description="True if the row cap was hit.")
    duration_ms: int
    format: Literal["compact"] = "compact"


class NonQueryResult(BaseModel):
    """Result of :func:`execute_non_query` (INSERT/UPDATE/DELETE/MERGE)."""

    model_config = ConfigDict(extra="forbid")

    rows_affected: int
    duration_ms: int


class DdlResult(BaseModel):
    """Result of :func:`execute_ddl`."""

    model_config = ConfigDict(extra="forbid")

    statement: str = Field(..., description="Leading DDL keyword (e.g. CREATE).")
    duration_ms: int


class ExplainResult(BaseModel):
    """Result of :func:`explain_query`."""

    model_config = ConfigDict(extra="forbid")

    plan_xml: str
    summary: str = Field(..., description="Short summary: operators + seeks/scans.")


# --- Schema introspection -------------------------------------------------


TableKind = Literal["BASE TABLE", "VIEW"]


class TableInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_name: Annotated[str, Field(alias="schema")]
    name: str
    type: TableKind
    row_count_estimate: int = Field(..., description="Estimate from DMV (can lag).")


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
        default_factory=list, description="PK column names, in key order."
    )
    sample_query: str = Field(..., description="Ready-to-run SELECT TOP (10).")


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
        default=None, description="From DMV; null if not granted."
    )


ForeignKeyDirection = Literal["outgoing", "incoming"]


class ForeignKeyInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    direction: ForeignKeyDirection = Field(
        ..., description="'outgoing': FK on this table; 'incoming': FK on others pointing here."
    )
    from_schema: str
    from_table: str
    from_columns: list[str]
    to_schema: str
    to_table: str
    to_columns: list[str]
    on_delete: str | None = None
    on_update: str | None = None


# --- Object inspection (procs / functions / views) ------------------------

ObjectKind = Literal["procedure", "scalar_function", "table_function", "view"]


class ObjectInfo(BaseModel):
    """One programmable object (proc / function / view) in a schema."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_name: Annotated[str, Field(alias="schema")]
    name: str
    kind: ObjectKind
    created: str | None = Field(default=None, description="ISO timestamp.")
    modified: str | None = Field(default=None, description="ISO timestamp.")


class ObjectDefinition(BaseModel):
    """The CREATE source text for one programmable object."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_name: Annotated[str, Field(alias="schema")]
    name: str
    kind: ObjectKind
    definition: str = Field(..., description="CREATE ... source from sys.sql_modules.")
    line_count: int


# --- Comparison results ---------------------------------------------------


class ObjectDiff(BaseModel):
    """Unified diff of one object's source across two environments."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    env_a: str
    env_b: str
    schema_name: Annotated[str, Field(alias="schema")]
    name: str
    kind: ObjectKind
    identical: bool = Field(..., description="True if source matches byte-for-byte.")
    unified_diff: str = Field(
        ..., description="Unified diff (env_a→env_b), empty when identical."
    )
    a_line_count: int
    b_line_count: int
    a_missing: bool = Field(default=False, description="Object doesn't exist in env_a.")
    b_missing: bool = Field(default=False, description="Object doesn't exist in env_b.")


class TableColumnDiff(BaseModel):
    """One column-level difference found by ``compare_table``."""

    model_config = ConfigDict(extra="forbid")

    column: str
    kind: Literal["added", "removed", "changed"]
    a: dict[str, Any] | None = None
    b: dict[str, Any] | None = None


class TableDiff(BaseModel):
    """Structural diff of one table across two environments."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    env_a: str
    env_b: str
    schema_name: Annotated[str, Field(alias="schema")]
    name: str
    identical: bool
    column_diffs: list[TableColumnDiff] = Field(default_factory=list)
    index_diffs: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Indexes present in one env but not the other, or with different shape.",
    )
    foreign_key_diffs: list[dict[str, Any]] = Field(default_factory=list)
    a_missing: bool = False
    b_missing: bool = False


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
    """One configured SQL Server target."""

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
    database: str
    description: str | None = None


class ServerInfo(BaseModel):
    """Snapshot of the connected server and the active MCP mode."""

    model_config = ConfigDict(extra="forbid")

    version: str
    edition: str | None = None
    database: str
    user: str = Field(..., description="SUSER_SNAME() — Entra principal under Entra modes.")
    original_login: str | None = Field(
        default=None, description="ORIGINAL_LOGIN(); differs after EXECUTE AS."
    )
    server_collation: str | None = None
    mode: ServerMode = Field(..., description="read_only / write / ddl.")
    auth_mode: AuthModeName = Field(..., description="Active auth flow.")
    max_rows: int
    query_timeout_seconds: int


# --- Helpers --------------------------------------------------------------


def to_param_tuple(params: list[QueryParam] | None) -> tuple[ParamValue, ...]:
    """Convert a list of :class:`QueryParam` to a positional ``?`` tuple."""
    if not params:
        return ()
    return tuple(p.value for p in params)
