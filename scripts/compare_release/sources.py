# -*- coding: utf-8 -*-
"""Load one side of a comparison into {OBJECT_NAME: definition_text}.

A "side" is whatever you are comparing: a consolidated release .sql file, a
definition dump pulled out of a database by the MCP server, or a directory of
one-object-per-file scripts. Every mode - db/db, db/file, file/file - is just a
choice of two sources.

This module never opens a database connection. Database sides arrive as dumps
produced by the MCP server, which owns the credentials and the read-only guard.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

PROC_HEADER_RE = re.compile(
    r"""^\s*
        (?:CREATE\s+(?:OR\s+ALTER\s+)?|ALTER\s+)
        (?:PROCEDURE|PROC|FUNCTION|VIEW)\s+
        (?:\[?(?P<schema>\w+)\]?\.)?
        \[?(?P<name>\w+)\]?
        \b
    """,
    re.IGNORECASE | re.VERBOSE,
)

SPLIT_HEADER_RE = re.compile(
    r"""^\s*
        (?:OR\s+ALTER\s+)?(?:PROCEDURE|PROC|FUNCTION|VIEW)\s+
        (?:\[?(?P<schema>\w+)\]?\.)?
        \[?(?P<name>\w+)\]?\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def find_object_definitions(text: str) -> dict[str, str]:
    """Return {NAME: final definition text} from a consolidated script.

    Last occurrence wins - release files often declare the same object twice and
    only the final one survives the deploy. Handles the split-line
    ``CREATE\\n OR ALTER PROCEDURE`` form by walking back to the bare ``CREATE``.
    """
    lines = text.splitlines(keepends=False)
    headers: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = PROC_HEADER_RE.match(line)
        if m:
            headers.append((i, m.group("name").upper()))
            continue
        m2 = SPLIT_HEADER_RE.match(line)
        if m2:
            j, steps, create_line = i - 1, 0, None
            while j >= 0 and steps < 5:
                prev = lines[j].strip()
                if prev:
                    if re.match(r"^CREATE\s*$", prev, re.IGNORECASE):
                        create_line = j
                    break
                j -= 1
                steps += 1
            if create_line is not None:
                headers.append((create_line, m2.group("name").upper()))

    bodies: dict[str, str] = {}
    for idx, (start, name) in enumerate(headers):
        upper_bound = headers[idx + 1][0] if idx + 1 < len(headers) else len(lines)
        end = upper_bound
        for j in range(start + 1, upper_bound):
            if re.match(r"^GO\s*(?:--.*)?$", lines[j].strip(), re.IGNORECASE):
                end = j
                break
        body = "\n".join(lines[start:end])
        body = re.sub(r"(?:\s*GO\s*)+\Z", "", body, flags=re.IGNORECASE)
        bodies[name] = body
    return bodies


def load_object_dump(path: Path) -> dict[str, str]:
    """Parse a definition dump produced by the MCP server's execute_query.

    Accepts either the MCP autosave envelope (``{"result": {"columns", "rows"}}``)
    or a plain ``{"NAME": "definition"}`` mapping. Column names are matched
    case-insensitively so ``proc_name``/``name``/``object_name`` and
    ``def``/``definition`` all work.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))

    if isinstance(payload, dict) and "result" not in payload and "columns" not in payload:
        return {str(k).upper(): v for k, v in payload.items() if v is not None}

    result = payload.get("result", payload)
    cols = [c.lower() for c in result["columns"]]
    rows = result["rows"]

    def pick(*candidates: str) -> int:
        for c in candidates:
            if c in cols:
                return cols.index(c)
        raise ValueError(
            f"dump is missing an expected column; it has {cols}, "
            f"and needs one of {candidates}"
        )

    name_idx = pick("proc_name", "object_name", "name")
    def_idx = pick("def", "definition")

    out: dict[str, str] = {}
    for row in rows:
        if isinstance(row, dict):
            keys = {k.lower(): k for k in row}
            nm, df = row[keys[cols[name_idx]]], row[keys[cols[def_idx]]]
        else:
            nm, df = row[name_idx], row[def_idx]
        if df is not None:
            out[str(nm).upper()] = df
    return out


def load_sql_dir(path: Path) -> dict[str, str]:
    """Every *.sql in a directory, keyed by filename stem."""
    return {
        p.stem.upper(): p.read_text(encoding="utf-8-sig", errors="replace")
        for p in sorted(path.glob("*.sql"))
    }


def load_source(spec: str) -> tuple[dict[str, str], str]:
    """Resolve a ``kind:path`` source spec.

    Supported kinds:
      ``file:PATH``  consolidated release script - objects are parsed out of it
      ``dump:PATH``  JSON definition dump from the MCP server (a database side)
      ``dir:PATH``   directory of one-object-per-file .sql scripts

    A bare path is treated as ``file:`` when it ends in .sql, else ``dump:``.
    Returns (objects, human-readable description).
    """
    if ":" in spec and not re.match(r"^[A-Za-z]:[\\/]", spec):
        kind, _, raw = spec.partition(":")
        kind = kind.lower()
    else:
        kind, raw = ("file" if spec.lower().endswith(".sql") else "dump"), spec

    p = Path(raw).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"source not found: {p}")

    if kind == "file":
        text = p.read_text(encoding="utf-8-sig", errors="replace")
        return find_object_definitions(text), f"{p.name} ({len(text.splitlines()):,} lines)"
    if kind == "dump":
        return load_object_dump(p), p.name
    if kind == "dir":
        return load_sql_dir(p), f"{p.name}/ ({len(list(p.glob('*.sql')))} files)"
    raise ValueError(f"unknown source kind {kind!r}; use file:, dump: or dir:")
