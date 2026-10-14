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
from dataclasses import dataclass, field
from pathlib import Path

PROC_HEADER_RE = re.compile(
    r"""^\s*
        (?:CREATE\s+(?:OR\s+ALTER\s+)?|ALTER\s+)
        (?:PROCEDURE|PROC|FUNCTION|VIEW|TRIGGER)\s+
        (?:\[?(?P<schema>\w+)\]?\.)?
        \[?(?P<name>\w+)\]?
        \b
    """,
    re.IGNORECASE | re.VERBOSE,
)

SPLIT_HEADER_RE = re.compile(
    r"""^\s*
        (?:OR\s+ALTER\s+)?(?:PROCEDURE|PROC|FUNCTION|VIEW|TRIGGER)\s+
        (?:\[?(?P<schema>\w+)\]?\.)?
        \[?(?P<name>\w+)\]?\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _module_headers(lines: list[str]) -> list[tuple[int, str]]:
    """[(0-based line index, UPPER name)] for every module header, in order.

    Handles the split-line ``CREATE\\n OR ALTER PROCEDURE`` form by walking back
    to the bare ``CREATE``.
    """
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
    return headers


def module_occurrences(text: str) -> dict[str, list[int]]:
    """{NAME: [1-based line of every definition]} - more than one is a duplicate."""
    out: dict[str, list[int]] = {}
    for i, name in _module_headers(text.splitlines(keepends=False)):
        out.setdefault(name, []).append(i + 1)
    return out


def find_object_definitions(text: str) -> dict[str, str]:
    """Return {NAME: final definition text} from a consolidated script.

    Last occurrence wins - release files often declare the same object twice and
    only the final one survives the deploy. Use `module_occurrences` to see the
    duplicates this collapses.
    """
    lines = text.splitlines(keepends=False)
    headers = _module_headers(lines)

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


# --------------------------------------------------------------------------- #
# Statements - everything in a script that is not a module definition
# --------------------------------------------------------------------------- #

_GO_RE = re.compile(r"^\s*GO\s*(?:\d+\s*)?(?:--.*)?$", re.IGNORECASE)
_STRING_RE = re.compile(r"N?'(?:[^']|'')*'")
_NAME = r"((?:\[[^\]]+\]|[\w#@]+)(?:\.(?:\[[^\]]*\]|[\w#@]*)){0,3})"
_TOP = r"(?:TOP\s*\(\s*\d+\s*\)\s+)?"

# (kind, pattern capturing the target, rank). Lower rank = more significant;
# a statement is labelled by its most significant action.
_ACTIONS = [
    ("CREATE TABLE", re.compile(r"\bCREATE\s+TABLE\s+" + _NAME, re.I), 0),
    ("ALTER TABLE", re.compile(r"\bALTER\s+TABLE\s+" + _NAME, re.I), 0),
    ("DROP TABLE", re.compile(r"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?" + _NAME, re.I), 0),
    ("TRUNCATE TABLE", re.compile(r"\bTRUNCATE\s+TABLE\s+" + _NAME, re.I), 0),
    ("FULLTEXT INDEX", re.compile(
        r"\b(?:CREATE|ALTER|DROP)\s+FULLTEXT\s+INDEX\s+ON\s+" + _NAME, re.I), 0),
    ("CREATE INDEX", re.compile(
        r"\bCREATE\s+(?:UNIQUE\s+)?(?:(?:NON)?CLUSTERED\s+)?(?:COLUMNSTORE\s+)?"
        r"INDEX\s+\S+\s+ON\s+" + _NAME, re.I), 0),
    ("ALTER INDEX", re.compile(r"\bALTER\s+INDEX\s+\S+\s+ON\s+" + _NAME, re.I), 0),
    ("DROP INDEX", re.compile(r"\bDROP\s+INDEX\s+(?:IF\s+EXISTS\s+)?" + _NAME, re.I), 0),
    ("CREATE TYPE", re.compile(r"\bCREATE\s+TYPE\s+" + _NAME, re.I), 0),
    ("CREATE SYNONYM", re.compile(r"\bCREATE\s+SYNONYM\s+" + _NAME, re.I), 0),
    ("CREATE SEQUENCE", re.compile(r"\bCREATE\s+SEQUENCE\s+" + _NAME, re.I), 0),
    ("CREATE SCHEMA", re.compile(r"\bCREATE\s+SCHEMA\s+" + _NAME, re.I), 0),
    ("DROP MODULE", re.compile(
        r"\bDROP\s+(?:PROCEDURE|PROC|FUNCTION|VIEW|TRIGGER|TYPE|SYNONYM)\s+"
        r"(?:IF\s+EXISTS\s+)?" + _NAME, re.I), 0),
    ("PERMISSION", re.compile(
        r"\b(?:GRANT|REVOKE|DENY)\s+\w+[^;]*?\bON\s+(?:OBJECT::)?" + _NAME, re.I), 0),
    ("MERGE", re.compile(r"\bMERGE\s+" + _TOP + r"(?:INTO\s+)?" + _NAME, re.I), 1),
    ("INSERT", re.compile(r"\bINSERT\s+" + _TOP + r"(?:INTO\s+)?" + _NAME, re.I), 1),
    ("UPDATE", re.compile(r"\bUPDATE\s+" + _TOP + _NAME + r"\s+SET\b", re.I), 1),
    ("DELETE", re.compile(r"\bDELETE\s+" + _TOP + r"(?:FROM\s+)?" + _NAME, re.I), 1),
    ("SELECT INTO", re.compile(
        r"\bSELECT\b(?:(?!\bSELECT\b|\bINSERT\b).)*?\bINTO\s+" + _NAME, re.I | re.S), 2),
    ("EXEC", re.compile(r"\bEXEC(?:UTE)?\s+(?!\()" + _NAME, re.I), 3),
    ("SELECT", re.compile(
        r"\bSELECT\b(?:(?!\bSELECT\b).)*?\bFROM\s+" + _NAME, re.I | re.S), 4),
    ("SELECT", re.compile(r"\bSELECT\b()", re.I), 4),
    ("PRINT", re.compile(r"\bPRINT\b()", re.I), 5),
]


def strip_comments(text: str) -> str:
    """The executable part of a batch: comments removed, string literals kept."""
    out, pos = [], 0
    for m in re.finditer(r"N?'(?:[^']|'')*'|/\*.*?\*/|--[^\n]*", text, re.S):
        out.append(text[pos:m.start()])
        tok = m.group(0)
        out.append(tok if tok.startswith(("'", "N'", "n'")) else " ")
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def code_key(text: str) -> str:
    """Comment- and whitespace-insensitive key for one statement batch."""
    return re.sub(r"\s+", " ", strip_comments(text)).strip()


def _is_scratch(target: str) -> bool:
    t = target.strip("[]")
    return t.startswith(("#", "@")) or not t


@dataclass
class Statement:
    """One GO-separated batch that is not a module definition."""
    origin: str          # file the batch came from
    line: int            # 1-based line of the batch's first content line
    text: str            # the batch exactly as written, without its GO
    kind: str            # most significant action, e.g. "ALTER TABLE"
    targets: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        tgt = self.targets[0] if self.targets else ""
        more = f" (+{len(self.targets) - 1} more)" if len(self.targets) > 1 else ""
        return f"{self.kind} {tgt}{more}".strip()

    @property
    def key(self) -> str:
        return code_key(self.text)


def _resolve_alias(code: str, pos: int, tgt: str) -> str:
    """``DELETE r FROM T r`` / ``UPDATE r SET ... FROM T r`` name the alias, not
    the table. Look for the alias in the FROM clause that follows."""
    if "." in tgt or "[" in tgt:
        return tgt
    m = re.search(r"\bFROM\s+" + _NAME + r"\s+(?:AS\s+)?" + re.escape(tgt) + r"\b",
                  code[pos:], re.I)
    return m.group(1) if m else tgt


def classify_statement(text: str) -> tuple[str, list[str], list[str]]:
    """(kind, targets, every action found) for one batch's executable text."""
    code = _STRING_RE.sub("''", strip_comments(text))
    found: list[tuple[int, int, str, str]] = []   # (rank, position, kind, target)
    for kind, rx, rank in _ACTIONS:
        for m in rx.finditer(code):
            tgt = m.group(1) or ""
            if kind in ("DELETE", "UPDATE"):
                tgt = _resolve_alias(code, m.end(), tgt)
            if kind in ("SELECT INTO", "INSERT", "UPDATE", "DELETE", "MERGE",
                        "CREATE TABLE", "DROP TABLE", "TRUNCATE TABLE") and _is_scratch(tgt):
                continue
            if kind == "SELECT" and not tgt and found:
                continue   # bare SELECT is only a fallback label
            found.append((rank, m.start(), kind, tgt))
    if not found:
        return "OTHER", [], []
    best_rank = min(f[0] for f in found)
    top = sorted((f for f in found if f[0] == best_rank), key=lambda f: f[1])
    targets: list[str] = []
    for f in top:
        if f[3] and f[3] not in targets:
            targets.append(f[3])
    actions = sorted({f[2] for f in found}, key=lambda k: min(x[1] for x in found if x[2] == k))
    return top[0][2], targets, actions


def split_batches(text: str) -> list[tuple[int, str]]:
    """[(1-based first content line, batch text)] split on GO lines.

    Empty batches are dropped; comment-only batches are kept so the caller can
    count them.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    out, buf, start = [], [], 0
    for i, ln in enumerate(lines + ["GO"]):
        if _GO_RE.match(ln):
            body = "\n".join(buf)
            if body.strip():
                first = next(j for j, x in enumerate(buf) if x.strip())
                out.append((start + first + 1, body.strip("\n")))
            buf, start = [], i + 1
        else:
            buf.append(ln)
    return out


def find_statements(text: str, origin: str = "") -> tuple[list[Statement], int]:
    """(statements, number of comment-only batches) - modules are excluded."""
    stmts: list[Statement] = []
    comment_only = 0
    for line, body in split_batches(text):
        if not code_key(body):
            comment_only += 1
            continue
        if _module_headers(body.split("\n")):
            continue
        kind, targets, actions = classify_statement(body)
        stmts.append(Statement(origin, line, body, kind, targets, actions))
    return stmts, comment_only


@dataclass
class Side:
    """One side of a comparison: its modules and, when known, its statements.

    ``statements`` is None for a database dump - a database has no record of
    which data or schema scripts were run, so those must be verified live.
    """
    objects: dict[str, str]
    desc: str
    statements: list[Statement] | None = None
    module_lines: dict[str, list[int]] = field(default_factory=dict)
    comment_only_batches: int = 0

    def duplicate_modules(self) -> dict[str, list[int]]:
        return {k: v for k, v in self.module_lines.items() if len(v) > 1}

    def duplicate_statements(self) -> list[list[Statement]]:
        groups: dict[str, list[Statement]] = {}
        for s in self.statements or []:
            groups.setdefault(s.key, []).append(s)
        return [g for g in groups.values() if len(g) > 1]


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


def _parse_spec(spec: str) -> tuple[str, Path]:
    if ":" in spec and not re.match(r"^[A-Za-z]:[\\/]", spec):
        kind, _, raw = spec.partition(":")
        kind = kind.lower()
    else:
        kind, raw = ("file" if spec.lower().endswith(".sql") else "dump"), spec
    p = Path(raw).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"source not found: {p}")
    if kind not in ("file", "dump", "dir"):
        raise ValueError(f"unknown source kind {kind!r}; use file:, dump: or dir:")
    return kind, p


def load_side(spec: str) -> Side:
    """Like `load_source`, but also returns every non-module statement and the
    line of every module definition, so nothing in a script goes unchecked."""
    kind, p = _parse_spec(spec)
    if kind == "dump":
        return Side(load_object_dump(p), p.name)
    texts = ([(p.name, p.read_text(encoding="utf-8-sig", errors="replace"))] if kind == "file"
             else [(f.name, f.read_text(encoding="utf-8-sig", errors="replace"))
                   for f in sorted(p.glob("*.sql"))])
    stmts: list[Statement] = []
    module_lines: dict[str, list[int]] = {}
    comment_only = 0
    for origin, text in texts:
        s, c = find_statements(text, origin)
        stmts.extend(s)
        comment_only += c
        for name, lines in module_occurrences(text).items():
            module_lines.setdefault(name, []).extend(lines)
    objects, desc = load_source(spec)
    return Side(objects, desc, stmts, module_lines, comment_only)


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
