"""Release runner - offline half of the ``sql-release-runner`` skill.

Sub-commands (stdlib only; never opens a database connection):

  plan       split ordered .sql files on GO, classify every batch, flag risks,
             skip batches already run successfully on the target (journal
             ledger) and duplicates, and write batch files + plan.json +
             preflight.md into a new run folder.
  remaining  list the batches of a run that still have no journal entry
             (the execution list to resume from).
  report     build the plain-English HTML report from one or more run
             folders and the server's journal (the source of truth).

Batches are executed by the MCP tool ``run_release_batch`` with
``batch_file=<run>/batches/<label>.sql`` and ``run_id=<run id>``; the
server reads the file itself and journals the full result, so nothing is
retyped and the report never depends on anyone's retelling."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

PLAN_VERSION = 1

# --------------------------------------------------------------------------
# reading & splitting


def read_sql_file(path: Path) -> tuple[str, str]:
    """Return (text, encoding). Handles UTF-8/UTF-16 BOMs, falls back to cp1252."""
    raw = path.read_bytes()
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
        if raw.startswith(bom):
            return raw.decode(enc), enc
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("cp1252"), "cp1252"


_GO_RE = re.compile(r"^[ \t]*GO(?:[ \t]+(\d+))?[ \t]*;?[ \t]*(?:--.*)?$", re.IGNORECASE)


@dataclass
class RawBatch:
    text: str
    start_line: int
    end_line: int
    repeat: int = 1


def split_batches(text: str) -> list[RawBatch]:
    """Split on GO lines that are not inside a block comment or string."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    batches: list[RawBatch] = []
    buf: list[str] = []
    start = 1
    in_block = 0
    in_str = False
    for idx, line in enumerate(lines, start=1):
        m = _GO_RE.match(line) if (in_block == 0 and not in_str) else None
        if m:
            batches.append(RawBatch("\n".join(buf), start, idx - 1, int(m.group(1) or 1)))
            buf = []
            start = idx + 1
            continue
        buf.append(line)
        i, n = 0, len(line)
        while i < n:
            ch, nxt = line[i], line[i + 1] if i + 1 < n else ""
            if in_str:
                if ch == "'":
                    if nxt == "'":
                        i += 2
                        continue
                    in_str = False
            elif in_block:
                if ch == "/" and nxt == "*":
                    in_block += 1
                    i += 1
                elif ch == "*" and nxt == "/":
                    in_block -= 1
                    i += 1
            elif ch == "-" and nxt == "-":
                break
            elif ch == "/" and nxt == "*":
                in_block += 1
                i += 1
            elif ch == "'":
                in_str = True
            i += 1
    batches.append(RawBatch("\n".join(buf), start, len(lines)))
    return batches


# --------------------------------------------------------------------------
# normalisation (must match mssql_mcp.release.sql_sha256)


def sql_sha256(sql: str) -> str:
    norm = "\n".join(line.rstrip() for line in sql.replace("\r\n", "\n").split("\n")).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def clean(sql: str, keep_names: bool = False) -> str:
    """Drop comments; blank out string literals; bracket names -> bare names
    (keep_names) or a placeholder."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch, nxt = sql[i], sql[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":
            while i < n and sql[i] != "\n":
                i += 1
        elif ch == "/" and nxt == "*":
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            out.append(" ")
        elif ch == "'":
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append("''")
        elif ch == "[":
            j = sql.find("]", i + 1)
            j = n - 1 if j < 0 else j
            out.append(sql[i + 1 : j].replace(" ", "_") if keep_names else "x")
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


# --------------------------------------------------------------------------
# classification

NAME = r"((?:\w+\s*\.\s*){0,2}#{0,2}\w+)"
_MODULE_RE = re.compile(
    r"^\s*(CREATE\s+OR\s+ALTER|CREATE|ALTER)\s+(PROC|PROCEDURE|FUNCTION|VIEW|TRIGGER)\s+" + NAME,
    re.IGNORECASE,
)
_SET_ONLY_RE = re.compile(
    r"^\s*(SET\s+(ANSI_NULLS|QUOTED_IDENTIFIER|ANSI_PADDING|ANSI_WARNINGS|ARITHABORT|"
    r"CONCAT_NULL_YIELDS_NULL|NUMERIC_ROUNDABORT|NOCOUNT|XACT_ABORT|ANSI_NULL_DFLT_ON)"
    r"\s+(ON|OFF)\s*;?\s*)+$",
    re.IGNORECASE,
)
_SET_ITEM_RE = re.compile(r"SET\s+(\w+)\s+(ON|OFF)", re.IGNORECASE)

_PATTERNS: dict[str, re.Pattern[str]] = {
    k: re.compile(v, re.IGNORECASE)
    for k, v in {
        "create_table": r"\bCREATE\s+TABLE\s+" + NAME,
        "alter_table": r"\bALTER\s+TABLE\s+" + NAME,
        "create_index": r"\bCREATE\s+(?:UNIQUE\s+)?(?:(?:NON)?CLUSTERED\s+)?INDEX\s+\w+\s+ON\s+" + NAME,
        "create_type": r"\bCREATE\s+(?:TYPE|SCHEMA|SEQUENCE|SYNONYM|ROLE)\s+" + NAME,
        "drop": r"\bDROP\s+(TABLE|VIEW|PROC|PROCEDURE|FUNCTION|TRIGGER|INDEX|TYPE|SCHEMA|SEQUENCE|SYNONYM)\s+(?:IF\s+EXISTS\s+)?"
        + NAME,
        "insert": r"\bINSERT\s+(?:INTO\s+)?" + NAME,
        "update": r"(?<!\bON\s)(?<!\bFOR\s)\bUPDATE\s+(?!STATISTICS\b)" + NAME,
        "delete": r"(?<!\bON\s)\bDELETE\s+(?:FROM\s+)?" + NAME,
        "merge": r"\bMERGE\s+(?:INTO\s+)?" + NAME,
        "truncate": r"\bTRUNCATE\s+TABLE\s+" + NAME,
        "exec": r"\bEXEC(?:UTE)?\s+(?!AS\b)(?:@\w+\s*=\s*)?" + NAME,
        "grant": r"\b(?:GRANT|REVOKE|DENY)\b",
        "select": r"\bSELECT\b",
    }.items()
}

_GUARD_RE = re.compile(
    r"\bIF\s+(?:NOT\s+)?EXISTS\b|\bOBJECT_ID\s*\(|\bCOL_LENGTH\s*\(|\bCOLUMNPROPERTY\s*\(|"
    r"\bINDEXPROPERTY\s*\(|\bTYPE_ID\s*\(|\bSCHEMA_ID\s*\(|\bsys\s*\.\s*(?:tables|columns|indexes|objects|"
    r"procedures|views|types|schemas|foreign_keys|default_constraints|check_constraints|key_constraints|"
    r"triggers|sequences)\b|\bINFORMATION_SCHEMA\b|\bDROP\s+\w+\s+IF\s+EXISTS\b|\bWHERE\s+NOT\s+EXISTS\b|"
    r"\bEXCEPT\b|\bIF\s+@@ROWCOUNT\b|\bIF\s*\(\s*SELECT\s+COUNT",
    re.IGNORECASE,
)

_STMT_STARTS = re.compile(
    r"\b(UPDATE|DELETE|INSERT|MERGE|CREATE|ALTER|DROP|IF|DECLARE|EXEC|EXECUTE|PRINT|COMMIT|ROLLBACK|"
    r"TRUNCATE|RETURN|WHILE|GRANT|BEGIN\s+TRAN\w*)\b|;",
    re.IGNORECASE,
)

_BLOCKED_HINTS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p, re.IGNORECASE | re.MULTILINE), msg)
    for p, msg in (
        (r"\bUSE\s+(?!HINT\b|PLAN\b)\w", "Contains USE <database>; the server will refuse it"),
        (r"\b(BACKUP|RESTORE)\b", "Contains BACKUP/RESTORE; the server will refuse it"),
        (r"\b(CREATE|ALTER|DROP)\s+(DATABASE|LOGIN)\b", "Database/login-level change; the server will refuse it"),
        (r"\bxp_cmdshell\b|\bOPENQUERY\b|\bOPENROWSET\b|\bOPENDATASOURCE\b", "External/OS access; the server will refuse it"),
        (r"\b\w+\s*\.\s*\w+\s*\.\s*\w*\s*\.\s*\w+\b", "Four-part (linked-server) name; the server will refuse it"),
        (r"\bDBCC\s+(?!CHECKIDENT\b)\w", "DBCC other than CHECKIDENT; the server will refuse it"),
    )
)
_SQLCMD_RE = re.compile(r"^\s*:(r|setvar|on\s+error|connect)\b|\$\(\w+\)", re.IGNORECASE | re.MULTILINE)
_THREE_PART_RE = re.compile(r"(?<![\w.#@])(\w+)\s*\.\s*(\w*)\s*\.\s*(\w+)(?![\w.])")


@dataclass
class Batch:
    label: str
    file_index: int
    file: str
    batch_index: int
    start_line: int
    end_line: int
    repeat: int
    sha256: str
    batch_file: str
    kind: str
    kind_label: str
    summary: str
    objects: list[str]
    action: str  # run | session | skip-empty | skip-already-ran | skip-duplicate
    action_note: str = ""
    session_settings: list[str] = field(default_factory=list)
    flags: list[dict[str, str]] = field(default_factory=list)
    line_count: int = 0


KIND_LABELS = {
    "procedure": "Stored procedure",
    "function": "Function",
    "view": "View",
    "trigger": "Trigger",
    "create_table": "New table",
    "alter_table": "Table change",
    "create_index": "Index",
    "create_type": "New type/schema",
    "drop": "Remove object",
    "insert": "Add data",
    "update": "Change data",
    "delete": "Delete data",
    "merge": "Sync data (MERGE)",
    "truncate": "Empty table",
    "exec": "Run procedure",
    "grant": "Permissions",
    "select": "Check/query only",
    "session": "Session setting",
    "empty": "Comments only",
    "other": "Other",
}
_KIND_PRIORITY = [
    "drop", "truncate", "create_table", "alter_table", "create_index", "create_type", "merge",
    "delete", "update", "insert", "exec", "grant", "select",
]
_VERB = {
    "create_table": "Create table", "alter_table": "Change table", "create_index": "Create index on",
    "create_type": "Create", "drop": "Remove", "insert": "Add rows to", "update": "Update rows in",
    "delete": "Delete rows from", "merge": "Sync rows in", "truncate": "Empty table", "exec": "Run",
}


def _name(raw: str) -> str:
    return re.sub(r"\s+", "", raw)


def _flag(batch: Batch, level: str, code: str, text: str) -> None:
    if not any(f["code"] == code for f in batch.flags):
        batch.flags.append({"level": level, "code": code, "text": text})


def _segments(cleaned: str, verb: str) -> list[str]:
    """Text of each ``verb`` statement up to the next statement start."""
    segs = []
    for m in re.finditer(rf"(?<!\bON\s)(?<!\bFOR\s)\b{verb}\b", cleaned, re.IGNORECASE):
        nxt = _STMT_STARTS.search(cleaned, m.end())
        segs.append(cleaned[m.start() : nxt.start() if nxt else len(cleaned)])
    return segs


def classify(batch: Batch, text: str, target_db: str) -> None:
    cleaned = clean(text)
    named = clean(text, keep_names=True)
    if not cleaned.strip():
        batch.kind, batch.action = "empty", "skip-empty"
        batch.action_note = "Only comments/blank lines - nothing to run"
    elif _SET_ONLY_RE.match(cleaned):
        batch.kind, batch.action = "session", "session"
        batch.action_note = "Applied to the following batches of this file (as SSMS would)"
    else:
        m = _MODULE_RE.match(named)
        if m:
            kind = m.group(2).upper()
            batch.kind = {"PROC": "procedure", "PROCEDURE": "procedure"}.get(kind, kind.lower())
            batch.objects = [_name(m.group(3))]
            verb = re.sub(r"\s+", " ", m.group(1).upper())
            batch.summary = f"{verb.capitalize()} {KIND_LABELS[batch.kind].lower()} {batch.objects[0]}"
            if verb == "CREATE":
                _flag(batch, "warn", "rerun", "Plain CREATE (not CREATE OR ALTER): fails if the object already exists")
        else:
            found: dict[str, list[str]] = {}
            for kind, pat in _PATTERNS.items():
                for mm in pat.finditer(named):
                    names = [g for g in mm.groups() if g and not g.upper() in {
                        "TABLE", "VIEW", "PROC", "PROCEDURE", "FUNCTION", "TRIGGER", "INDEX",
                        "TYPE", "SCHEMA", "SEQUENCE", "SYNONYM"}] if mm.groups() else []
                    found.setdefault(kind, [])
                    if names:
                        nm = _name(names[-1])
                        if nm.upper() not in {"SET", "FROM", "INTO", "TOP"} and nm not in found[kind]:
                            found[kind].append(nm)
            kinds = [k for k in _KIND_PRIORITY if k in found]
            batch.kind = kinds[0] if kinds else "other"
            objs: list[str] = []
            for k in kinds:
                for o in found[k]:
                    if o not in objs and not o.startswith("@"):
                        objs.append(o)
            batch.objects = objs[:8]
            parts = []
            for k in kinds:
                if k in _VERB and found[k]:
                    parts.append(f"{_VERB[k]} {', '.join(found[k][:3])}")
                elif k == "grant":
                    parts.append("Grant/revoke permissions")
                elif k == "select" and len(kinds) == 1:
                    parts.append("Read-only query (changes nothing)")
            batch.summary = "; ".join(parts[:4]) or KIND_LABELS.get(batch.kind, "Other")

            # temp tables (#t) and table variables never need re-run or backup warnings
            real = {k: [o for o in v if not o.startswith(("#", "@"))] for k, v in found.items()}
            real = {k: v for k, v in real.items() if v or k in ("grant", "select")}
            perm_tbl = r"\bALTER\s+TABLE\s+[^#@\s]\S*\s+"
            guarded = bool(_GUARD_RE.search(cleaned))
            ddl_create = any(k in real for k in ("create_table", "create_index", "create_type"))
            alter_add = bool(re.search(perm_tbl + r"(?:WITH\s+\w+\s+)?ADD\b", cleaned, re.IGNORECASE))
            if (ddl_create or alter_add) and not guarded:
                _flag(batch, "warn", "rerun", "No IF EXISTS / IF NOT EXISTS check: fails or duplicates if run twice")
            if "insert" in real and not guarded:
                _flag(batch, "warn", "rerun", "INSERT without an existence check: running it twice adds the rows twice")
            if any(k in real for k in ("update", "delete")) and not guarded:
                _flag(batch, "info", "rerun", "UPDATE/DELETE without an IF EXISTS check (usually safe to repeat; confirm)")

            for verb, label in (("UPDATE", "UPDATE"), ("DELETE", "DELETE")):
                for seg in _segments(cleaned, verb):
                    if (re.match(rf"{verb}\s*\(", seg, re.IGNORECASE)
                            or re.match(rf"{verb}\s+(?:FROM\s+)?[#@]", seg, re.IGNORECASE)
                            or re.match(r"UPDATE\s+STATISTICS", seg, re.IGNORECASE)):
                        continue
                    if not re.search(r"\bWHERE\b|\bCURRENT\s+OF\b", seg, re.IGNORECASE) and not re.search(r"\bJOIN\b", seg, re.IGNORECASE):
                        _flag(batch, "danger", "nowhere", f"{label} without a WHERE clause: affects every row of the table")
            if "drop" in real:
                _flag(batch, "danger", "drop", "Removes objects: " + ", ".join(real["drop"][:5]))
            if "truncate" in real:
                _flag(batch, "danger", "truncate", "TRUNCATE removes all rows: " + ", ".join(real["truncate"][:5]))
            if re.search(perm_tbl + r"DROP\s+(COLUMN|CONSTRAINT)?", cleaned, re.IGNORECASE):
                _flag(batch, "danger", "dropcol", "ALTER TABLE ... DROP removes a column/constraint")
            touches = [k for k in ("update", "delete", "merge", "truncate") if k in real]
            if touches or re.search(perm_tbl + r"(DROP|ALTER\s+COLUMN)\b", cleaned, re.IGNORECASE) or "drop" in real:
                tbls = sorted({o for k in touches for o in real[k]})
                _flag(batch, "backup", "backup",
                      "Changes or removes existing data" + (f" ({', '.join(tbls[:5])})" if tbls else "")
                      + " - confirm a backup exists before running (no backup is taken by this tool)")

            created_tmp = {m.lower() for m in re.findall(r"\bCREATE\s+TABLE\s+(#\w+)", cleaned, re.IGNORECASE)}
            created_tmp |= {m.lower() for m in re.findall(r"\bINTO\s+(#\w+)", cleaned, re.IGNORECASE)}
            used_tmp = {m.lower() for m in re.findall(r"(?<!#)(#\w+)", cleaned)}
            missing_tmp = used_tmp - created_tmp
            if missing_tmp:
                _flag(batch, "danger", "tempx",
                      f"Uses temp table(s) {', '.join(sorted(missing_tmp))} created in another batch - each batch runs on its own, so this will fail")

        for pat, msg in _BLOCKED_HINTS:
            if pat.search(cleaned):
                _flag(batch, "danger", "blocked", msg)
        if re.search(r"\bEXEC(?:UTE)?\s*\(|\bsp_executesql\b", cleaned, re.IGNORECASE):
            _flag(batch, "info", "dynsql", "Uses dynamic SQL - the safety checks cannot see inside it; review the text")
        if _SQLCMD_RE.search(text):
            _flag(batch, "danger", "sqlcmd", "Uses SQLCMD-mode syntax (:r, :setvar, $(var)) which is not supported")
        for m in _THREE_PART_RE.finditer(named):
            db = m.group(1)
            if db.lower() not in {target_db.lower(), "sys", "dbo", "tempdb"} and not db.lower().startswith("information_schema"):
                if re.fullmatch(r"\d+", db):
                    continue
                _flag(batch, "warn", "xdb", f"References another database ({db}); writes there are refused if it belongs to another environment")
                break
    if batch.repeat > 1:
        _flag(batch, "warn", "repeat", f"GO {batch.repeat}: this batch is meant to run {batch.repeat} times")
    batch.kind_label = KIND_LABELS.get(batch.kind, "Other")
    if not batch.summary:
        batch.summary = batch.kind_label


# --------------------------------------------------------------------------
# ledger (journal of previous runs - the server's own records)


def load_journal(journal_dir: Path) -> list[dict]:
    rows: list[dict] = []
    if not journal_dir.is_dir():
        return rows
    for f in sorted(journal_dir.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


# --------------------------------------------------------------------------
# plan


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:60] or "release"


def cmd_plan(args: argparse.Namespace) -> int:
    files: list[Path] = [Path(f) for f in args.files or []]
    if args.order_file:
        base = Path(args.order_file).parent
        for line in Path(args.order_file).read_text(encoding="utf-8-sig").splitlines():
            line = line.strip().strip('"')
            if line and not line.startswith("#"):
                p = Path(line)
                files.append(p if p.is_absolute() else base / p)
    if not files:
        print("No input files given (use --files or --order-file).", file=sys.stderr)
        return 2
    missing = [str(f) for f in files if not f.is_file()]
    if missing:
        print("Missing files:\n  " + "\n  ".join(missing), file=sys.stderr)
        return 2

    root = Path(args.root)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_id = args.run_id or f"{stamp}-{_slug(args.env)}-{_slug(args.name)}"
    run_dir = root / run_id
    if run_dir.exists():
        print(f"Run folder already exists: {run_dir}", file=sys.stderr)
        return 2
    (run_dir / "batches").mkdir(parents=True)

    ledger: dict[str, dict] = {}
    for row in load_journal(Path(args.journal_dir)):
        if row.get("environment") == args.env and row.get("status") == "succeeded":
            ledger.setdefault(row.get("sql_sha256", ""), row)

    seen: dict[str, str] = {}
    batches: list[Batch] = []
    file_meta = []
    for fi, path in enumerate(files, start=1):
        text, enc = read_sql_file(path)
        raw = split_batches(text)
        file_meta.append({"index": fi, "path": str(path.resolve()), "name": path.name,
                          "encoding": enc, "batches": len(raw), "lines": text.count("\n") + 1})
        session: dict[str, str] = {}
        for bi, rb in enumerate(raw, start=1):
            label = f"F{fi:02d}-B{bi:03d}"
            sha = sql_sha256(rb.text)
            b = Batch(label=label, file_index=fi, file=path.name, batch_index=bi,
                      start_line=rb.start_line, end_line=rb.end_line, repeat=rb.repeat, sha256=sha,
                      batch_file=f"{run_id}/batches/{label}.sql", kind="other", kind_label="",
                      summary="", objects=[], action="run",
                      line_count=max(0, rb.end_line - rb.start_line + 1))
            classify(b, rb.text, args.database or "")
            if b.action == "session":
                for opt, val in _SET_ITEM_RE.findall(clean(rb.text)):
                    session[opt.upper()] = val.upper()
            elif b.action == "run":
                b.session_settings = [f"SET {k} {v}" for k, v in session.items()]
                if sha in ledger:
                    prev = ledger[sha]
                    b.action = "skip-already-ran"
                    b.action_note = (f"Identical batch already ran successfully on {args.env} "
                                     f"({prev.get('started_at', '')[:16].replace('T', ' ')} UTC, run {prev.get('run_id', '?')})")
                elif sha in seen and args.skip_duplicates:
                    b.action = "skip-duplicate"
                    b.action_note = f"Exact duplicate of {seen[sha]} earlier in this release"
                else:
                    seen.setdefault(sha, label)
            if b.action in ("run", "session"):
                (run_dir / "batches" / f"{label}.sql").write_text(rb.text, encoding="utf-8")
            batches.append(b)

    plan = {
        "plan_version": PLAN_VERSION,
        "run_id": run_id,
        "release": args.name,
        "environment": args.env,
        "database": args.database,
        "created": datetime.now().isoformat(timespec="seconds"),
        "root": str(root.resolve()),
        "journal_dir": str(Path(args.journal_dir).resolve()),
        "files": file_meta,
        "batches": [asdict(b) for b in batches],
    }
    (run_dir / "plan.json").write_text(json.dumps(plan, indent=1), encoding="utf-8")
    (run_dir / "preflight.md").write_text(preflight_md(plan), encoding="utf-8")
    print(preflight_console(plan, run_dir))
    return 0


def _counts(plan: dict) -> dict[str, int]:
    c: dict[str, int] = {}
    for b in plan["batches"]:
        c[b["action"]] = c.get(b["action"], 0) + 1
    return c


def preflight_console(plan: dict, run_dir: Path) -> str:
    c = _counts(plan)
    flags: dict[str, int] = {}
    for b in plan["batches"]:
        if b["action"] == "run":
            for f in b["flags"]:
                flags[f["code"]] = flags.get(f["code"], 0) + 1
    lines = [
        f"RUN_ID      {plan['run_id']}",
        f"RUN_DIR     {run_dir.resolve()}",
        f"TARGET      {plan['environment']} / {plan['database']}",
        f"FILES       {len(plan['files'])}   BATCHES {len(plan['batches'])}",
        f"TO RUN      {c.get('run', 0)}   session-settings {c.get('session', 0)}   "
        f"skip: already-ran {c.get('skip-already-ran', 0)}, duplicate {c.get('skip-duplicate', 0)}, empty {c.get('skip-empty', 0)}",
        "FLAGS (batches to run): " + (", ".join(f"{k}={v}" for k, v in sorted(flags.items())) or "none"),
        f"PREFLIGHT   {(run_dir / 'preflight.md').resolve()}",
    ]
    return "\n".join(lines)


_FLAG_TITLES = {
    "blocked": "Will be refused by the server",
    "sqlcmd": "Unsupported SQLCMD syntax",
    "tempx": "Temp table from another batch (will fail)",
    "nowhere": "UPDATE/DELETE without WHERE",
    "drop": "Removes objects",
    "truncate": "Empties tables",
    "dropcol": "Drops columns/constraints",
    "backup": "Changes existing data - confirm backup",
    "rerun": "No re-run safety check",
    "xdb": "References another database",
    "repeat": "GO <n> repeat count",
    "dynsql": "Dynamic SQL (not inspected)",
}


def preflight_md(plan: dict) -> str:
    c = _counts(plan)
    out = [f"# Pre-flight - {plan['release']} -> {plan['environment']} ({plan['database']})", "",
           f"Run id: `{plan['run_id']}`  ", f"Created: {plan['created']}", "",
           "| Files | Batches | To run | Session settings | Skip: already ran | Skip: duplicate | Skip: empty |",
           "|---|---|---|---|---|---|---|",
           f"| {len(plan['files'])} | {len(plan['batches'])} | {c.get('run', 0)} | {c.get('session', 0)} | "
           f"{c.get('skip-already-ran', 0)} | {c.get('skip-duplicate', 0)} | {c.get('skip-empty', 0)} |", "",
           "## Files (run order)", ""]
    for f in plan["files"]:
        out.append(f"{f['index']}. `{f['name']}` - {f['batches']} batches, {f['lines']} lines ({f['encoding']})")
    out += ["", "## Needs your decision before running", ""]
    order = ["blocked", "sqlcmd", "tempx", "nowhere", "drop", "truncate", "dropcol", "backup", "rerun", "xdb",
             "repeat", "dynsql"]
    any_flag = False
    for code in order:
        rows = [(b, f) for b in plan["batches"] if b["action"] == "run" for f in b["flags"] if f["code"] == code]
        if not rows:
            continue
        any_flag = True
        out += [f"### {_FLAG_TITLES[code]} ({len(rows)})", ""]
        for b, f in rows:
            out.append(f"- **{b['label']}** `{b['file']}` lines {b['start_line']}-{b['end_line']}: {b['summary']} - {f['text']}")
        out.append("")
    if not any_flag:
        out += ["Nothing flagged.", ""]
    skipped = [b for b in plan["batches"] if b["action"].startswith("skip-already") or b["action"] == "skip-duplicate"]
    if skipped:
        out += ["## Will be skipped (same query already ran / duplicate)", ""]
        for b in skipped:
            out.append(f"- {b['label']} `{b['file']}` lines {b['start_line']}-{b['end_line']}: {b['summary']} - {b['action_note']}")
        out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------
# remaining (execution list)


def load_decisions(run_dir: Path) -> dict[str, str]:
    """{label: reason} for batches the user chose not to run."""
    f = run_dir / "decisions.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}


def cmd_exclude(args: argparse.Namespace) -> int:
    run_dir = Path(args.run)
    plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
    known = {b["label"] for b in plan["batches"]}
    labels = [x.strip() for x in args.labels.split(",") if x.strip()]
    unknown = [x for x in labels if x not in known]
    if unknown:
        print("Unknown labels: " + ", ".join(unknown), file=sys.stderr)
        return 2
    decisions = load_decisions(run_dir)
    for label in labels:
        if args.undo:
            decisions.pop(label, None)
        else:
            decisions[label] = args.reason
    (run_dir / "decisions.json").write_text(json.dumps(decisions, indent=1), encoding="utf-8")
    print(f"EXCLUDED {len(decisions)}: " + ", ".join(sorted(decisions)))
    return 0


def cmd_remaining(args: argparse.Namespace) -> int:
    run_dir = Path(args.run)
    plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
    done = {r.get("label") for r in load_journal(Path(plan["journal_dir"])) if r.get("run_id") == plan["run_id"]}
    excluded = load_decisions(run_dir)
    todo = [b for b in plan["batches"]
            if b["action"] == "run" and b["label"] not in done and b["label"] not in excluded]
    print(f"# run_id={plan['run_id']} environment={plan['environment']} "
          f"remaining={len(todo)} done={len(done)} excluded={len(excluded)}")
    for b in todo[: args.limit or None]:
        print(json.dumps({"label": b["label"], "batch_file": b["batch_file"],
                          "session_settings": b["session_settings"], "repeat": b["repeat"]}))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from report import build_report  # local module beside this file

    runs = []
    for r in args.run:
        run_dir = Path(r)
        plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
        journal = [j for j in load_journal(Path(plan["journal_dir"])) if j.get("run_id") == plan["run_id"]]
        excluded = load_decisions(run_dir)
        runs.append({"plan": plan, "journal": journal, "excluded": excluded, "run_dir": str(run_dir.resolve())})
    out = Path(args.out)
    out.write_text(build_report(runs, title=args.title), encoding="utf-8")
    print(f"REPORT {out.resolve()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--files", nargs="*")
    p.add_argument("--order-file")
    p.add_argument("--env", required=True)
    p.add_argument("--database", required=True)
    p.add_argument("--name", required=True, help="release name, e.g. UW-CMS2-26.08.01.00")
    p.add_argument("--root", required=True, help="release batch root (MSSQL_RELEASE_BATCH_ROOT)")
    p.add_argument("--journal-dir", required=True)
    p.add_argument("--run-id")
    p.add_argument("--no-skip-duplicates", dest="skip_duplicates", action="store_false")
    p.set_defaults(func=cmd_plan)
    x = sub.add_parser("exclude", help="record batches the user chose not to run")
    x.add_argument("--run", required=True)
    x.add_argument("--labels", required=True)
    x.add_argument("--reason", default="Excluded by user at pre-flight review")
    x.add_argument("--undo", action="store_true")
    x.set_defaults(func=cmd_exclude)
    r = sub.add_parser("remaining")
    r.add_argument("--run", required=True)
    r.add_argument("--limit", type=int, default=0)
    r.set_defaults(func=cmd_remaining)
    rep = sub.add_parser("report")
    rep.add_argument("--run", action="append", required=True)
    rep.add_argument("--out", required=True)
    rep.add_argument("--title", default="Database Script Run Report")
    rep.set_defaults(func=cmd_report)
    args = ap.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
