"""Merge an MCP server entry into Claude Desktop's config file.

Shared by ``install.ps1`` (during initial setup) and
``register-cowork.ps1`` (for later refreshes). Doing the JSON merge in
Python sidesteps every PowerShell 5.1 PSCustomObject / OrderedDictionary /
smart-quote landmine we hit.

Usage:
    python merge_cowork_entry.py <config_path> <entry_name> <entry_file> <force> <dry_run>

Where:
    <config_path>: path to claude_desktop_config.json
    <entry_name>:  e.g. "mssql"
    <entry_file>:  path to a JSON file with the entry to merge
    <force>:       "true" or "false" — overwrite an existing entry
    <dry_run>:     "true" or "false" — print, don't write

Exit codes:
    0 — merge succeeded (or dry-run completed)
    1 — usage error / IO failure
    2 — entry already exists and force=false
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time


def _usage() -> None:
    print(
        "usage: merge_cowork_entry.py "
        "<config_path> <entry_name> <entry_file> <force> <dry_run>",
        file=sys.stderr,
    )
    sys.exit(1)


def main() -> int:
    if len(sys.argv) != 6:
        _usage()
    config_path, entry_name, entry_file, force_arg, dry_run_arg = sys.argv[1:]
    force = force_arg == "true"
    dry_run = dry_run_arg == "true"

    with open(entry_file, encoding="utf-8") as fh:
        entry = json.load(fh)

    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as fh:
            text = fh.read().strip()
        config = json.loads(text) if text else {}
    else:
        config = {}

    servers = config.setdefault("mcpServers", {})
    existing_names = sorted(servers.keys())
    print(
        "existing_servers="
        + (",".join(existing_names) if existing_names else "(none)")
    )

    already_present = entry_name in servers
    if already_present and not force:
        print("status=skip-already-present")
        print("---existing-entry---")
        print(json.dumps(servers[entry_name], indent=2))
        return 2

    servers[entry_name] = entry
    print(
        "status=will-" + ("overwrite" if already_present else "add")
    )
    print("---entry-being-written---")
    print(json.dumps(entry, indent=2))
    print("---final-mcp-servers---")
    print(",".join(sorted(servers.keys())))

    if dry_run:
        print("status=dry-run-no-write")
        return 0

    if os.path.exists(config_path):
        backup = config_path + ".bak-" + time.strftime("%Y%m%d%H%M%S")
        shutil.copy2(config_path, backup)
        print("backup=" + backup)
    else:
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

    with open(config_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(config, fh, indent=2)
    print("wrote=" + config_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
