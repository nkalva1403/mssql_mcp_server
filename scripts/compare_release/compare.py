"""Compare a consolidated SQL release file against a target environment.

Read-only. Produces a clickable HTML drift report plus per-object change
blocks (added / only-in-staging / changed) with real SQL text and
surrounding context, so you can see what would actually deploy.

Pairs with the ``sql-compare`` Claude skill in this repo: the skill
orchestrates the staging dump via the running MCP server, then invokes
this script for the actual diff + rendering. The script itself never
opens a database connection — it works offline against a JSON dump.

Usage
-----
    python compare.py --file CONSOLIDATED.sql --staging-dump DUMP.txt \\
                      --out-dir OUTDIR [--open]

The ``--staging-dump`` file is the on-disk result the MCP server saves
when ``execute_query`` returns more data than fits inline. Produce it
with a single query like::

    SELECT o.name AS proc_name, m.definition AS def
    FROM sys.sql_modules m
    INNER JOIN sys.objects o ON o.object_id = m.object_id
    INNER JOIN sys.schemas s ON s.schema_id = o.schema_id
    WHERE s.name = 'dbo' AND o.name IN (<names from file>)
    ORDER BY o.name;

with ``format='compact'``. The MCP autosaves the response when oversized.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import html
import json
import os
import re
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

KEYWORDS = sorted(
    [
        "SELECT", "FROM", "WHERE", "INNER JOIN", "LEFT JOIN", "LEFT OUTER JOIN",
        "RIGHT JOIN", "RIGHT OUTER JOIN", "CROSS JOIN", "CROSS APPLY",
        "OUTER APPLY", "GROUP BY", "ORDER BY", "HAVING", "UNION", "UNION ALL",
        "INTERSECT", "EXCEPT", "WITH", "INSERT", "UPDATE", "DELETE", "MERGE",
        "DECLARE", "SET", "IF", "ELSE", "BEGIN", "END", "WHILE", "RETURN",
        "EXEC", "EXECUTE", "RAISERROR", "THROW", "CREATE", "ALTER", "DROP",
        "AS", "PIVOT", "UNPIVOT", "OPENJSON", "CASE", "WHEN", "THEN", "PRINT",
        "TRUNCATE", "ON", "AND", "OR", "VALUES", "INTO", "OUTPUT", "TOP",
    ],
    key=len,
    reverse=True,
)

CONTEXT_LINES = 3

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


# --------------------------------------------------------------------------- #
# Reformat / normalize
# --------------------------------------------------------------------------- #


def strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def reformat(sql: str) -> str:
    """Make line-by-line diff readable: one keyword per line, lowercase."""
    s = strip_comments(sql)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"\[(\w+)\]", r"\1", s)
    s = re.sub(r"\s+", " ", s)
    pattern = r"\b(" + "|".join(re.escape(k) for k in KEYWORDS) + r")\b"
    s = re.sub(pattern, lambda m: "\n" + m.group(1), s, flags=re.IGNORECASE)
    s = re.sub(r";", ";\n", s)
    out: list[str] = []
    for line in s.split("\n"):
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            out.append(line.lower())
    return "\n".join(out)


def normalize(sql: str) -> str:
    """Aggressive normalize for equality check (ignores all whitespace)."""
    s = strip_comments(sql)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"\[(\w+)\]", r"\1", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    s = re.sub(
        r"^\s*(?:create\s+(?:or\s+alter\s+)?|or\s+alter\s+|alter\s+)?"
        r"(procedure|function|view)\s+",
        r"create \1 ",
        s,
    )
    return s


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# File parsing
# --------------------------------------------------------------------------- #


def find_object_definitions(text: str) -> dict[str, str]:
    """Return {name_upper: final_definition_text}. Last occurrence wins.

    Handles split-line ``CREATE\\n OR ALTER PROCEDURE`` by walking back to the
    bare ``CREATE`` line and using that as the start of the body.
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


# --------------------------------------------------------------------------- #
# Staging dump
# --------------------------------------------------------------------------- #


def load_staging_dump(path: Path) -> dict[str, str]:
    """Parse the JSON the MCP server saves when execute_query overflows.

    Expects ``result.columns`` to contain (case-insensitive) at least one
    "name"-like column and one "definition"-like column. Picks the first
    column matching ``proc_name|name|object_name`` and the first matching
    ``def|definition``.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    result = payload.get("result", payload)
    cols = [c.lower() for c in result["columns"]]
    rows = result["rows"]

    def pick(*candidates: str) -> int:
        for c in candidates:
            if c in cols:
                return cols.index(c)
        raise ValueError(
            f"staging dump missing expected column; have {cols}, "
            f"need one of {candidates}"
        )

    name_idx = pick("proc_name", "object_name", "name")
    def_idx = pick("def", "definition")
    return {
        row[name_idx].upper(): row[def_idx]
        for row in rows
        if row[def_idx] is not None
    }


# --------------------------------------------------------------------------- #
# Diff
# --------------------------------------------------------------------------- #


def extract_changes(staging_text: str, file_text: str) -> list[dict]:
    a = staging_text.splitlines()
    b = file_text.splitlines()
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    opcodes = sm.get_opcodes()
    blocks: list[dict] = []
    for k, (tag, i1, i2, j1, j2) in enumerate(opcodes):
        if tag == "equal":
            continue
        ctx_before: list[str] = []
        for prev_tag, pi1, pi2, *_ in reversed(opcodes[:k]):
            if prev_tag == "equal":
                ctx_before = a[pi1:pi2][-CONTEXT_LINES:]
                break
        ctx_after: list[str] = []
        for nxt_tag, ni1, ni2, *_ in opcodes[k + 1 :]:
            if nxt_tag == "equal":
                ctx_after = a[ni1:ni2][:CONTEXT_LINES]
                break

        if tag == "insert":
            kind, sl, fl = "ADDED", [], b[j1:j2]
        elif tag == "delete":
            kind, sl, fl = "REMOVED", a[i1:i2], []
        else:
            kind, sl, fl = "CHANGED", a[i1:i2], b[j1:j2]
        blocks.append(
            {
                "kind": kind,
                "staging_lines": sl,
                "file_lines": fl,
                "staging_pos": i1 + 1,
                "file_pos": j1 + 1,
                "context_before": ctx_before,
                "context_after": ctx_after,
            }
        )
    return blocks


def audit_dates(sql: str) -> list[str]:
    pats = [r"\b\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\b", r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b"]
    found: list[str] = []
    for p in pats:
        found.extend(re.findall(p, sql))
    seen, out = set(), []
    for d in found:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


# --------------------------------------------------------------------------- #
# Render: HTML
# --------------------------------------------------------------------------- #

CSS = """
:root{--bg:#fff;--fg:#24292f;--muted:#57606a;--border:#d0d7de;--hdr-bg:#f6f8fa;
--add-bg:#e6ffec;--add-fg:#1a7f37;--add-bd:#1a7f37;
--del-bg:#ffebe9;--del-fg:#b3261e;--del-bd:#b3261e;
--chg-bg:#fff8c5;--chg-fg:#7a5c00;--chg-bd:#bf8700;--link:#0969da;--code:#0d1117;}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#c9d1d9;--muted:#8b949e;
--border:#30363d;--hdr-bg:#161b22;
--add-bg:#033a16;--add-fg:#56d364;--add-bd:#238636;
--del-bg:#67060c;--del-fg:#ffa198;--del-bd:#da3633;
--chg-bg:#3e2e00;--chg-fg:#ffd866;--chg-bd:#bf8700;--link:#58a6ff;--code:#161b22;}}
html,body{background:var(--bg);color:var(--fg);margin:0;padding:0;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;}
.container{max-width:1400px;margin:0 auto;padding:16px 20px 80px;}
h1{font-size:20px;margin:8px 0 4px;}
h2{font-size:15px;margin:24px 0 8px;color:var(--muted);font-weight:600;}
.toolbar{display:flex;flex-wrap:wrap;gap:16px;align-items:center;
padding:8px 0 16px;border-bottom:1px solid var(--border);}
.pill{display:inline-block;padding:2px 10px;border-radius:999px;
font-size:12px;font-weight:600;}
.pill.match{background:#d8f5d8;color:#1a7f37;}
.pill.drift{background:#fff1c2;color:#7a5c00;}
.pill.danger{background:#ffd7d5;color:#b3261e;}
@media (prefers-color-scheme:dark){
.pill.match{background:#033a16;color:#56d364;}
.pill.drift{background:#3e2e00;color:#ffd866;}
.pill.danger{background:#67060c;color:#ffa198;}}
a{color:var(--link);text-decoration:none;}a:hover{text-decoration:underline;}
.meta{color:var(--muted);font-size:13px;}
.summary{font-size:13px;margin:8px 0 16px;}.summary span{margin-right:14px;}
.change{margin:14px 0;border:1px solid var(--border);border-radius:6px;overflow:hidden;}
.change>.hdr{padding:8px 12px;font-size:13px;font-weight:600;display:flex;gap:10px;
align-items:center;background:var(--hdr-bg);border-bottom:1px solid var(--border);}
.change.added>.hdr{border-left:4px solid var(--add-bd);}
.change.removed>.hdr{border-left:4px solid var(--del-bd);}
.change.changed>.hdr{border-left:4px solid var(--chg-bd);}
.kindlabel{padding:2px 8px;border-radius:4px;font-size:11px;letter-spacing:.4px;}
.kindlabel.added{background:var(--add-bg);color:var(--add-fg);}
.kindlabel.removed{background:var(--del-bg);color:var(--del-fg);}
.kindlabel.changed{background:var(--chg-bg);color:var(--chg-fg);}
.pos{color:var(--muted);font-size:12px;font-weight:400;}
.note{color:var(--muted);font-size:12px;font-weight:400;}
.code{font-family:ui-monospace,"SFMono-Regular",Menlo,Consolas,monospace;
font-size:12.5px;background:var(--code);padding:10px 12px;white-space:pre-wrap;
word-break:break-word;line-height:1.45;}
.code.added{background:var(--add-bg);color:var(--add-fg);}
.code.removed{background:var(--del-bg);color:var(--del-fg);}
.code.ctx{background:transparent;color:var(--muted);
border-top:1px dashed var(--border);border-bottom:1px dashed var(--border);font-size:11.5px;}
.label{font-size:11px;color:var(--muted);padding:6px 12px 0;text-transform:uppercase;
letter-spacing:.6px;}
table.idx{border-collapse:collapse;width:100%;font-size:13px;}
table.idx th,table.idx td{border:1px solid var(--border);padding:6px 10px;text-align:left;}
table.idx th{background:var(--hdr-bg);}table.idx td.r{text-align:right;}
"""


def render_proc_html(
    name: str, blocks: list[dict], staging_ahead: bool, file_chars: int, stg_chars: int
) -> str:
    c = {"ADDED": 0, "REMOVED": 0, "CHANGED": 0}
    for b in blocks:
        c[b["kind"]] += 1
    pill = (
        '<span class="pill danger">STAGING AHEAD - file would regress</span>'
        if staging_ahead
        else '<span class="pill drift">DRIFT</span>'
    )
    parts: list[str] = [
        f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        f'<title>{html.escape(name)} - changes</title><style>{CSS}</style>'
        f'</head><body><div class="container">'
        f'<div class="toolbar"><a href="index.html">&larr; index</a>'
        f'<h1 style="margin:0;">{html.escape(name)}</h1>{pill}</div>'
        f'<p class="meta">File body: {file_chars:,} chars &middot; '
        f'Staging body: {stg_chars:,} chars &middot; '
        f'Δ {file_chars - stg_chars:+,}</p>'
        f'<p class="summary">'
        f'<span><strong>{c["ADDED"]}</strong> blocks <em>added in file</em></span>'
        f'<span><strong>{c["REMOVED"]}</strong> blocks <em>only in staging</em></span>'
        f'<span><strong>{c["CHANGED"]}</strong> blocks <em>changed</em></span></p>'
        f'<p class="note">Reformatted before diff: comments stripped, '
        f'brackets stripped, one keyword per line. Cosmetic whitespace is '
        f'hidden so every block below is a real semantic change.</p>'
    ]
    for i, b in enumerate(blocks, 1):
        kind = b["kind"]
        ctx_b = (
            f'<div class="label">context (preceding, identical)</div>'
            f'<div class="code ctx">{html.escape(chr(10).join(b["context_before"]))}</div>'
            if b["context_before"] else ""
        )
        ctx_a = (
            f'<div class="label">context (following, identical)</div>'
            f'<div class="code ctx">{html.escape(chr(10).join(b["context_after"]))}</div>'
            if b["context_after"] else ""
        )
        if kind == "ADDED":
            inner = (
                f'<div class="label">added (file only)</div>'
                f'<div class="code added">{html.escape(chr(10).join(b["file_lines"]))}</div>'
            )
            note = "new code introduced in the file"
            cls = "added"
            label = '<span class="kindlabel added">ADDED IN FILE</span>'
        elif kind == "REMOVED":
            inner = (
                f'<div class="label">missing from file</div>'
                f'<div class="code removed">{html.escape(chr(10).join(b["staging_lines"]))}</div>'
            )
            note = "WOULD BE LOST if the file is deployed as-is"
            cls = "removed"
            label = '<span class="kindlabel removed">ONLY IN STAGING</span>'
        else:
            inner = (
                f'<div class="label">staging (current)</div>'
                f'<div class="code removed">{html.escape(chr(10).join(b["staging_lines"]))}</div>'
                f'<div class="label">file (replacement)</div>'
                f'<div class="code added">{html.escape(chr(10).join(b["file_lines"]))}</div>'
            )
            note = "this block was rewritten in the file"
            cls = "changed"
            label = '<span class="kindlabel changed">CHANGED</span>'
        parts.append(
            f'<div class="change {cls}"><div class="hdr">#{i} {label}'
            f'<span class="pos">staging line {b["staging_pos"]} '
            f'&nbsp;|&nbsp; file line {b["file_pos"]}</span>'
            f'<span class="note">{note}</span></div>'
            f'{ctx_b}{inner}{ctx_a}</div>'
        )
    parts.append("</div></body></html>")
    return "\n".join(parts)


def render_index_html(rows: list[dict], file_name: str, env_label: str) -> str:
    body_rows = []
    for r in rows:
        link = (
            f'<a href="{r["name"]}.html">{r["name"]}</a>'
            if r["status"] == "DIFFERS"
            else r["name"]
        )
        if r["status"] == "MATCH":
            pill = '<span class="pill match">MATCH</span>'
        elif r["status"] == "MISSING":
            pill = '<span class="pill danger">missing</span>'
        elif r.get("staging_ahead"):
            pill = '<span class="pill danger">staging ahead</span>'
        else:
            pill = '<span class="pill drift">DRIFT</span>'
        a = r.get("added", 0)
        rm = r.get("removed", 0)
        ch = r.get("changed", 0)
        body_rows.append(
            f"<tr><td>{link}</td><td>{pill}</td>"
            f"<td class='r'>{a}</td><td class='r'>{rm}</td>"
            f"<td class='r'>{ch}</td></tr>"
        )
    summary = {
        "MATCH": sum(1 for r in rows if r["status"] == "MATCH"),
        "DRIFT": sum(1 for r in rows if r["status"] == "DIFFERS"),
        "AHEAD": sum(1 for r in rows if r.get("staging_ahead")),
        "MISSING": sum(1 for r in rows if r["status"] == "MISSING"),
    }
    return (
        f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        f'<title>Drift report</title><style>{CSS}</style></head><body>'
        f'<div class="container">'
        f'<h1>Drift report &mdash; consolidated file vs {html.escape(env_label)}</h1>'
        f'<p class="meta">File: <code>{html.escape(file_name)}</code> '
        f'(read-only compare)</p>'
        f'<p class="summary">'
        f'<span class="pill match">MATCH</span> {summary["MATCH"]} '
        f'&nbsp;<span class="pill drift">DRIFT</span> {summary["DRIFT"]} '
        f'&nbsp;<span class="pill danger">staging ahead</span> {summary["AHEAD"]} '
        f'&nbsp;<span class="pill danger">missing</span> {summary["MISSING"]}</p>'
        f'<table class="idx"><thead><tr><th>Object</th><th>Status</th>'
        f'<th class="r"># added (file only)</th>'
        f'<th class="r"># only in staging</th>'
        f'<th class="r"># changed</th></tr></thead><tbody>'
        f'{chr(10).join(body_rows)}</tbody></table>'
        f'<p class="meta" style="margin-top:24px;">Click any DRIFT row '
        f'to see the exact added / removed / changed SQL.</p>'
        f"</div></body></html>"
    )


# --------------------------------------------------------------------------- #
# Render: plain text
# --------------------------------------------------------------------------- #


def render_text(name: str, blocks: list[dict], staging_ahead: bool) -> str:
    out: list[str] = []
    bar = "=" * 78
    out.append(bar)
    out.append(f" {name}")
    out.append(
        " STAGING IS AHEAD - deploying the file would REGRESS these changes"
        if staging_ahead
        else " File has the changes - staging is the older state"
    )
    out.append(bar + "\n")
    c = {"ADDED": 0, "REMOVED": 0, "CHANGED": 0}
    for b in blocks:
        c[b["kind"]] += 1
    out.append(
        f"Change blocks: {c['ADDED']} added (in file), "
        f"{c['REMOVED']} removed (only in staging), {c['CHANGED']} changed\n"
    )
    for i, b in enumerate(blocks, 1):
        kind = b["kind"]
        out.append("-" * 78)
        if kind == "ADDED":
            out.append(
                f"#{i}  ADDED IN FILE   (around staging line "
                f"{b['staging_pos']}, file line {b['file_pos']})"
            )
            out.append("       -> new code in the file")
        elif kind == "REMOVED":
            out.append(
                f"#{i}  ONLY IN STAGING (around staging line "
                f"{b['staging_pos']}, file line {b['file_pos']})"
            )
            out.append("       -> WOULD BE LOST if the file is deployed as-is")
        else:
            out.append(
                f"#{i}  CHANGED         (staging line {b['staging_pos']}, "
                f"file line {b['file_pos']})"
            )
        out.append("")
        if b["context_before"]:
            out.append("  context (preceding, identical):")
            for line in b["context_before"]:
                out.append(f"      {line}")
            out.append("")
        if kind == "ADDED":
            out.append("  + new in file:")
            for line in b["file_lines"]:
                out.append(f"  +   {line}")
        elif kind == "REMOVED":
            out.append("  - only in staging:")
            for line in b["staging_lines"]:
                out.append(f"  -   {line}")
        else:
            out.append("  staging (current):")
            for line in b["staging_lines"]:
                out.append(f"    -   {line}")
            out.append("  file (replacement):")
            for line in b["file_lines"]:
                out.append(f"    +   {line}")
        if b["context_after"]:
            out.append("")
            out.append("  context (following, identical):")
            for line in b["context_after"]:
                out.append(f"      {line}")
        out.append("")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def open_in_browser(path: Path) -> None:
    """Best-effort cross-platform open."""
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # noqa: S606
        elif sys.platform == "darwin":
            os.system(f'open "{path}"')  # noqa: S605
        else:
            os.system(f'xdg-open "{path}"')  # noqa: S605
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--file",
        required=True,
        type=Path,
        help="Consolidated SQL file (the 'desired state')",
    )
    parser.add_argument(
        "--staging-dump",
        required=True,
        type=Path,
        help="JSON dump from MCP execute_query (the 'current state')",
    )
    parser.add_argument(
        "--out-dir",
        required=True,
        type=Path,
        help="Where to write the HTML / text report",
    )
    parser.add_argument(
        "--env-label",
        default="staging",
        help="Label to show in the report (default: 'staging')",
    )
    parser.add_argument(
        "--open", action="store_true", help="Open index.html in the default browser"
    )
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)

    file_text = args.file.read_text(encoding="utf-8", errors="replace")
    file_bodies = find_object_definitions(file_text)
    print(
        f"  parsed {len(file_bodies)} objects from file: {args.file.name}",
        file=sys.stderr,
    )

    staging_bodies = load_staging_dump(args.staging_dump)
    print(
        f"  loaded {len(staging_bodies)} staging defs from dump",
        file=sys.stderr,
    )

    results: list[dict] = []
    for name in sorted(file_bodies.keys()):
        file_body = file_bodies[name]
        staging_body = staging_bodies.get(name)
        entry: dict = {
            "name": name,
            "file_chars": len(file_body),
            "staging_chars": len(staging_body) if staging_body else None,
        }
        if staging_body is None:
            entry["status"] = "MISSING"
            results.append(entry)
            continue
        if normalize(file_body) == normalize(staging_body):
            entry["status"] = "MATCH"
            results.append(entry)
            continue
        entry["status"] = "DIFFERS"
        blocks = extract_changes(reformat(staging_body), reformat(file_body))
        c = {"ADDED": 0, "REMOVED": 0, "CHANGED": 0}
        for b in blocks:
            c[b["kind"]] += 1
        entry.update(
            added=c["ADDED"],
            removed=c["REMOVED"],
            changed=c["CHANGED"],
        )
        # Audit-date heuristic for "staging ahead" detection
        f_dates = set(audit_dates(file_body))
        s_dates = set(audit_dates(staging_body))
        entry["dates_only_in_file"] = sorted(f_dates - s_dates)
        entry["dates_only_in_staging"] = sorted(s_dates - f_dates)
        entry["staging_ahead"] = bool(entry["dates_only_in_staging"])
        # Render text + HTML
        page = render_proc_html(
            name, blocks, entry["staging_ahead"], len(file_body), len(staging_body)
        )
        (args.out_dir / f"{name}.html").write_text(page, encoding="utf-8")
        text = render_text(name, blocks, entry["staging_ahead"])
        (args.out_dir / f"{name}.txt").write_text(text, encoding="utf-8")
        results.append(entry)

    # Index + JSON
    index_html = render_index_html(results, args.file.name, args.env_label)
    (args.out_dir / "index.html").write_text(index_html, encoding="utf-8")
    summary = {
        "total": len(results),
        "match": sum(1 for r in results if r["status"] == "MATCH"),
        "differs": sum(1 for r in results if r["status"] == "DIFFERS"),
        "missing": sum(1 for r in results if r["status"] == "MISSING"),
        "staging_ahead": sum(1 for r in results if r.get("staging_ahead")),
    }
    (args.out_dir / "report.json").write_text(
        json.dumps({"summary": summary, "results": results}, indent=2),
        encoding="utf-8",
    )

    print(json.dumps(summary, indent=2))
    print(f"  HTML index: {args.out_dir / 'index.html'}", file=sys.stderr)

    if args.open:
        open_in_browser(args.out_dir / "index.html")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
