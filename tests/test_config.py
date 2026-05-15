"""Tests for :mod:`mssql_mcp.config`."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from mssql_mcp.config import Settings, get_settings


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip every MSSQL_* / MCP_* / AZURE_* var so we can probe required-field errors."""
    for key in list(os.environ):
        if key.startswith(("MSSQL_", "MCP_", "AZURE_")):
            monkeypatch.delenv(key, raising=False)
    yield


def _base(**kwargs: object) -> Settings:
    """Build a Settings instance bypassing the .env file."""
    defaults: dict[str, object] = {
        "mssql_server": "localhost",
        "mssql_database": "TestDb",
    }
    defaults.update(kwargs)
    return Settings(_env_file=None, **defaults)  # type: ignore[arg-type,call-arg]


def test_missing_required_env_raises(clean_env: None) -> None:
    """No server / database in the environment should fail validation."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)  # type: ignore[call-arg]
    msg = str(exc_info.value).lower()
    assert "mssql_server" in msg
    assert "mssql_database" in msg


def test_default_auth_mode_is_entra_default(clean_env: None) -> None:
    """Per spec section 2, the default auth mode is ``entra_default``."""
    settings = _base()
    assert settings.mssql_auth_mode == "entra_default"
    assert settings.is_entra_auth is True


def test_read_only_false_flips_flag(clean_env: None) -> None:
    settings = _base(mssql_read_only=False)
    assert settings.mssql_read_only is False


# --- SQL auth ------------------------------------------------------------


def test_sql_auth_requires_credentials(clean_env: None) -> None:
    with pytest.raises(ValidationError) as exc_info:
        _base(mssql_auth_mode="sql")
    assert "MSSQL_USERNAME" in str(exc_info.value)


def test_sql_auth_connection_string_has_credentials(clean_env: None) -> None:
    settings = _base(
        mssql_auth_mode="sql", mssql_username="u", mssql_password="p"
    )
    cs = settings.connection_string()
    assert "UID=u" in cs
    assert "PWD=p" in cs


def test_safe_connection_string_redacts_password(clean_env: None) -> None:
    settings = _base(
        mssql_auth_mode="sql", mssql_username="u", mssql_password="hunter2"
    )
    safe = settings.safe_connection_string()
    assert "hunter2" not in safe
    assert "PWD=***" in safe


# --- Windows auth --------------------------------------------------------


def test_windows_auth_connection_string(clean_env: None) -> None:
    settings = _base(mssql_auth_mode="windows")
    cs = settings.connection_string()
    assert "Trusted_Connection=yes" in cs
    assert "UID=" not in cs
    assert "PWD=" not in cs


# --- Entra: service principal --------------------------------------------


def test_entra_service_principal_requires_all_three_vars(
    clean_env: None,
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        _base(mssql_auth_mode="entra_service_principal")
    msg = str(exc_info.value)
    assert "AZURE_TENANT_ID" in msg
    assert "AZURE_CLIENT_ID" in msg
    assert "AZURE_CLIENT_SECRET" in msg


def test_entra_service_principal_accepts_all_three(clean_env: None) -> None:
    settings = _base(
        mssql_auth_mode="entra_service_principal",
        azure_tenant_id="tid",
        azure_client_id="cid",
        azure_client_secret="csec",
    )
    assert settings.mssql_auth_mode == "entra_service_principal"


# --- Entra: service principal with cert ----------------------------------


def test_entra_sp_cert_requires_cert_path(clean_env: None) -> None:
    with pytest.raises(ValidationError) as exc_info:
        _base(
            mssql_auth_mode="entra_service_principal_cert",
            azure_tenant_id="t",
            azure_client_id="c",
        )
    assert "AZURE_CLIENT_CERTIFICATE_PATH" in str(exc_info.value)


def test_entra_sp_cert_rejects_missing_file(
    clean_env: None, tmp_path: Path
) -> None:
    missing = tmp_path / "nope.pem"
    with pytest.raises(ValidationError) as exc_info:
        _base(
            mssql_auth_mode="entra_service_principal_cert",
            azure_tenant_id="t",
            azure_client_id="c",
            azure_client_certificate_path=missing,
        )
    assert "does not exist" in str(exc_info.value)


def test_entra_sp_cert_accepts_existing_file(
    clean_env: None, tmp_path: Path
) -> None:
    cert = tmp_path / "cert.pem"
    cert.write_text("dummy")
    settings = _base(
        mssql_auth_mode="entra_service_principal_cert",
        azure_tenant_id="t",
        azure_client_id="c",
        azure_client_certificate_path=cert,
    )
    assert settings.azure_client_certificate_path == cert


# --- Entra: interactive / device code ------------------------------------


def test_entra_interactive_works_without_tenant_id(clean_env: None) -> None:
    """Tenant ID is *optional* for interactive flows — MSAL falls back to
    the ``organizations`` endpoint and lets any work account sign in."""
    settings = _base(mssql_auth_mode="entra_interactive")
    assert settings.mssql_auth_mode == "entra_interactive"
    assert settings.azure_tenant_id is None


def test_entra_device_code_works_without_tenant_id(clean_env: None) -> None:
    settings = _base(mssql_auth_mode="entra_device_code")
    assert settings.mssql_auth_mode == "entra_device_code"


def test_entra_interactive_accepts_explicit_tenant(clean_env: None) -> None:
    """Explicit tenant pins the sign-in flow to that tenant only."""
    settings = _base(
        mssql_auth_mode="entra_interactive", azure_tenant_id="tid"
    )
    assert settings.azure_tenant_id == "tid"


# --- Entra: cli / managed_identity / default need nothing extra ----------


@pytest.mark.parametrize(
    "mode", ["entra_cli", "entra_managed_identity", "entra_default"]
)
def test_entra_modes_with_no_extra_vars(clean_env: None, mode: str) -> None:
    settings = _base(mssql_auth_mode=mode)  # type: ignore[arg-type]
    assert settings.mssql_auth_mode == mode


# --- Connection strings for Entra modes contain NO credentials ---------------


@pytest.mark.parametrize(
    "mode,extras",
    [
        ("entra_default", {}),
        ("entra_cli", {}),
        ("entra_managed_identity", {}),
        (
            "entra_service_principal",
            {
                "azure_tenant_id": "t",
                "azure_client_id": "c",
                "azure_client_secret": "s",
            },
        ),
    ],
)
def test_entra_connection_string_contains_no_credentials(
    clean_env: None, mode: str, extras: dict[str, str]
) -> None:
    settings = _base(mssql_auth_mode=mode, **extras)  # type: ignore[arg-type]
    cs = settings.connection_string()
    assert "PWD=" not in cs
    assert "UID=" not in cs
    assert "Authentication=" not in cs
    assert "Trusted_Connection" not in cs


# --- Behaviour / formatting ----------------------------------------------


def test_log_level_is_uppercased(clean_env: None) -> None:
    settings = _base(log_level="debug")
    assert settings.log_level == "DEBUG"


def test_ddl_requires_writes_enabled(clean_env: None) -> None:
    with pytest.raises(ValidationError) as exc_info:
        _base(mssql_read_only=True, mssql_allow_ddl=True)
    assert "MSSQL_ALLOW_DDL" in str(exc_info.value)


def test_get_settings_uses_environment_defaults() -> None:
    """``get_settings()`` succeeds under the conftest env (sql auth set up)."""
    settings = get_settings()
    assert isinstance(settings, Settings)


def test_token_cache_path_expands_tilde(clean_env: None) -> None:
    settings = _base(mssql_token_cache_path="~/cache.bin")
    assert "~" not in str(settings.mssql_token_cache_path)
