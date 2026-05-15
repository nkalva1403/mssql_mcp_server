"""Entry point: ``python -m mssql_mcp`` and the ``mssql-mcp-server`` console script.

Dispatches subcommands so ``init`` / ``doctor`` work even before the
environment is fully configured (those don't import the server module,
which would otherwise fail on missing env vars)."""

from __future__ import annotations

import sys

from mssql_mcp.cli import dispatch


def main() -> None:
    exit_code = dispatch(sys.argv[1:])
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
