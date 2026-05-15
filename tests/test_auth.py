"""Unit tests for :mod:`mssql_mcp.auth` — credential dispatch, token
encoding, cache file permissions, and the no-token-logging guarantee.

These tests **never** make a real Entra ID call. The ``azure-identity``
credential classes are replaced with simple recording fakes."""

from __future__ import annotations

import json
import os
import struct
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from azure.core.credentials import AccessToken

from mssql_mcp import audit, auth
from mssql_mcp.config import Settings

# --- Encoder -------------------------------------------------------------


def test_encode_token_uses_utf16le_with_length_prefix() -> None:
    token = "hello"
    encoded = auth.encode_token_for_pyodbc(token)
    expected_payload = token.encode("utf-16-le")
    expected = struct.pack("<i", len(expected_payload)) + expected_payload
    assert encoded == expected
    # length prefix is little-endian: 'hello' = 10 bytes UTF-16-LE
    assert encoded[:4] == (10).to_bytes(4, "little")


def test_encode_token_roundtrip_preserves_value() -> None:
    token = "abc.def.ghi"
    encoded = auth.encode_token_for_pyodbc(token)
    length = struct.unpack("<i", encoded[:4])[0]
    payload = encoded[4:]
    assert length == len(payload)
    assert payload.decode("utf-16-le") == token


# --- Fake credential -----------------------------------------------------


class _FakeCredential:
    """Deterministic AccessToken producer for tests."""

    def __init__(self, expires_in: int = 3600, token: str = "fake-jwt") -> None:
        self._expires_in = expires_in
        self._token = token
        self.calls: int = 0

    def get_token(self, *scopes: str, **kwargs: object) -> AccessToken:
        self.calls += 1
        return AccessToken(self._token, int(time.time()) + self._expires_in)


def _entra_settings(mode: str, **extras: object) -> Settings:
    base: dict[str, object] = {
        "mssql_server": "x.database.windows.net",
        "mssql_database": "Db",
        "mssql_auth_mode": mode,
    }
    base.update(extras)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type,call-arg]


# --- Provider basics -----------------------------------------------------


def test_token_provider_rejects_non_entra_mode() -> None:
    sql_settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        mssql_server="x",
        mssql_database="d",
        mssql_auth_mode="sql",
        mssql_username="u",
        mssql_password="p",
    )
    with pytest.raises(ValueError, match="Entra auth"):
        auth.TokenProvider(sql_settings)


def test_get_token_returns_struct_and_audits(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    audit.configure(log_path)

    settings = _entra_settings(
        "entra_service_principal",
        azure_tenant_id="11111111-2222-3333-4444-deadbeefcafe",
        azure_client_id="aaaaaaaa-bbbb-cccc-dddd-feedfaced3f4",
        azure_client_secret="s",
    )
    fake = _FakeCredential()
    provider = auth.TokenProvider(settings, credential=fake)

    struct_ = provider.get_token()
    assert fake.calls == 1
    assert struct_.bytes[:4] == struct.pack(
        "<i", len("fake-jwt".encode("utf-16-le"))
    )
    assert struct_.expires_on > datetime.now(UTC)

    for handler in audit.get_logger().handlers:
        handler.flush()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    records = [json.loads(line) for line in lines]
    token_records = [r for r in records if r["event"] == "token_acquired"]
    assert len(token_records) == 1
    rec = token_records[0]
    assert rec["auth_mode"] == "entra_service_principal"
    assert rec["client_id_suffix"] == "...d3f4"
    assert rec["tenant_id_suffix"] == "...cafe"


def test_token_value_never_appears_in_audit(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    audit.configure(log_path)

    settings = _entra_settings("entra_cli")
    fake = _FakeCredential(token="SUPER-SECRET-TOKEN-VALUE-XYZ")
    provider = auth.TokenProvider(settings, credential=fake)
    provider.get_token()

    for handler in audit.get_logger().handlers:
        handler.flush()
    body = log_path.read_text(encoding="utf-8")
    assert "SUPER-SECRET-TOKEN-VALUE-XYZ" not in body


# --- needs_refresh -------------------------------------------------------


def test_needs_refresh_true_within_buffer() -> None:
    now = datetime(2026, 5, 14, 10, 0, 0, tzinfo=UTC)
    expiry = now + timedelta(minutes=2)
    assert auth.TokenProvider.needs_refresh(expiry, now=now) is True


def test_needs_refresh_false_outside_buffer() -> None:
    now = datetime(2026, 5, 14, 10, 0, 0, tzinfo=UTC)
    expiry = now + timedelta(minutes=30)
    assert auth.TokenProvider.needs_refresh(expiry, now=now) is False


# --- Credential dispatch -------------------------------------------------


def test_dispatch_service_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _Captured:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def get_token(self, *_: object, **__: object) -> AccessToken:
            return AccessToken("t", int(time.time()) + 3600)

    monkeypatch.setattr(auth, "ClientSecretCredential", _Captured)
    settings = _entra_settings(
        "entra_service_principal",
        azure_tenant_id="t",
        azure_client_id="c",
        azure_client_secret="s",
    )
    auth.TokenProvider(settings)
    assert captured == {"tenant_id": "t", "client_id": "c", "client_secret": "s"}


def test_dispatch_certificate_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cert = tmp_path / "x.pem"
    cert.write_text("dummy")
    captured: dict[str, object] = {}

    class _Captured:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def get_token(self, *_: object, **__: object) -> AccessToken:
            return AccessToken("t", int(time.time()) + 3600)

    monkeypatch.setattr(auth, "CertificateCredential", _Captured)
    settings = _entra_settings(
        "entra_service_principal_cert",
        azure_tenant_id="t",
        azure_client_id="c",
        azure_client_certificate_path=cert,
        azure_client_certificate_password="pw",
    )
    auth.TokenProvider(settings)
    assert captured["tenant_id"] == "t"
    assert captured["client_id"] == "c"
    assert captured["certificate_path"] == str(cert)
    assert captured["password"] == "pw"


def test_dispatch_managed_identity_user_assigned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _Captured:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def get_token(self, *_: object, **__: object) -> AccessToken:
            return AccessToken("t", int(time.time()) + 3600)

    monkeypatch.setattr(auth, "ManagedIdentityCredential", _Captured)
    settings = _entra_settings(
        "entra_managed_identity", azure_client_id="user-assigned"
    )
    auth.TokenProvider(settings)
    assert captured == {"client_id": "user-assigned"}


def test_dispatch_managed_identity_system_assigned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _Captured:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def get_token(self, *_: object, **__: object) -> AccessToken:
            return AccessToken("t", int(time.time()) + 3600)

    monkeypatch.setattr(auth, "ManagedIdentityCredential", _Captured)
    settings = _entra_settings("entra_managed_identity")
    auth.TokenProvider(settings)
    # No client_id forwarded -> system-assigned MI
    assert captured == {}


def test_dispatch_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    instantiated = []

    class _Captured:
        def __init__(self) -> None:
            instantiated.append(self)

        def get_token(self, *_: object, **__: object) -> AccessToken:
            return AccessToken("t", int(time.time()) + 3600)

    monkeypatch.setattr(auth, "AzureCliCredential", _Captured)
    auth.TokenProvider(_entra_settings("entra_cli"))
    assert len(instantiated) == 1


def test_dispatch_default(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _Captured:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def get_token(self, *_: object, **__: object) -> AccessToken:
            return AccessToken("t", int(time.time()) + 3600)

    monkeypatch.setattr(auth, "DefaultAzureCredential", _Captured)
    auth.TokenProvider(_entra_settings("entra_default"))
    assert captured == {"exclude_interactive_browser_credential": False}


def test_dispatch_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _Captured:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def get_token(self, *_: object, **__: object) -> AccessToken:
            return AccessToken("t", int(time.time()) + 3600)

    monkeypatch.setattr(auth, "InteractiveBrowserCredential", _Captured)
    auth.TokenProvider(
        _entra_settings(
            "entra_interactive",
            azure_tenant_id="tid",
            mssql_disable_token_cache=True,
        )
    )
    assert captured["tenant_id"] == "tid"


def test_dispatch_device_code(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _Captured:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def get_token(self, *_: object, **__: object) -> AccessToken:
            return AccessToken("t", int(time.time()) + 3600)

    monkeypatch.setattr(auth, "DeviceCodeCredential", _Captured)
    auth.TokenProvider(
        _entra_settings(
            "entra_device_code",
            azure_tenant_id="tid",
            mssql_disable_token_cache=True,
        )
    )
    assert captured["tenant_id"] == "tid"


# --- Repeated get_token() consults the credential each call --------------


def test_repeat_get_token_consults_credential(tmp_path: Path) -> None:
    audit.configure(tmp_path / "audit.jsonl")
    settings = _entra_settings("entra_cli")
    fake = _FakeCredential()
    provider = auth.TokenProvider(settings, credential=fake)
    provider.get_token()
    provider.get_token()
    provider.get_token()
    assert fake.calls == 3


# --- Token cache permissions ---------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only chmod check")
def test_verify_token_cache_rejects_world_readable(tmp_path: Path) -> None:
    cache = tmp_path / "cache.bin"
    cache.write_bytes(b"x")
    os.chmod(cache, 0o644)
    with pytest.raises(PermissionError, match="overly permissive"):
        auth.verify_token_cache_permissions(cache)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only chmod check")
def test_verify_token_cache_accepts_0600(tmp_path: Path) -> None:
    cache = tmp_path / "cache.bin"
    cache.write_bytes(b"x")
    os.chmod(cache, 0o600)
    auth.verify_token_cache_permissions(cache)  # no raise


def test_verify_token_cache_missing_file_is_noop(tmp_path: Path) -> None:
    auth.verify_token_cache_permissions(tmp_path / "no-such-file")
