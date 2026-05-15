"""Shared pytest fixtures and environment defaults.

We populate the bare-minimum required env vars *before* any test module
imports :mod:`mssql_mcp.config`, so ``Settings()`` constructions inside
tests succeed unless the test explicitly clears env vars to probe error
paths.
"""

from __future__ import annotations

import os

_DEFAULTS = {
    "MSSQL_SERVER": "localhost",
    "MSSQL_DATABASE": "TestDb",
    "MSSQL_AUTH_MODE": "sql",
    "MSSQL_USERNAME": "tester",
    "MSSQL_PASSWORD": "test-pass",
    "MSSQL_READ_ONLY": "true",
    "MSSQL_ALLOW_DDL": "false",
    "MSSQL_AUDIT_LOG_PATH": "./logs/test_audit.jsonl",
}

for _key, _value in _DEFAULTS.items():
    os.environ.setdefault(_key, _value)
