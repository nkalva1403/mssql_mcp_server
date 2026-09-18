# -*- coding: utf-8 -*-
"""Locked comparison-report format (v1.1).

This module is the single source of truth for how SQL comparison reports look
and how differences are decided. Every comparison mode - database to database,
database to consolidated file, file to file - renders through `Report` so the
output is identical in structure, colour and wording.

Do not fork this file per comparison type. If a mode needs something new, add it
here as a component and bump FORMAT_VERSION.

SQL Server does not give a module back the way it was written, so both sides are
put into the same shape first. Formatting alone must never read as a change:

  * `CREATE OR ALTER X` is stored as `CREATE    X`, and `PROC` may become
    `PROCEDURE`. Canonicalised on both sides.
  * The header itself can come back split over several lines with blanks in
    between. It is merged into a single entry on both sides before aligning,
    keeping the line number of where the statement really starts.
  * A release script's separator rule often ends up stored above the header.
    That residue is set aside and counted, not reported as a difference.
  * Indentation, trailing space and blank lines are not preserved. Excluded from
    the comparison and counted, so the line totals still reconcile.

Everything that survives those rules is a real difference.

Read-only by construction: nothing in here talks to a database.
"""
from __future__ import annotations

import difflib
import hashlib
import html
import re
from dataclasses import dataclass, field

FORMAT_VERSION = "1.1"

E = html.escape

# --------------------------------------------------------------------------- #
# Difference engine - shared by every mode
# --------------------------------------------------------------------------- #

_WS = re.compile(r"[ ]+")
_CREATE_PROC = re.compile(r"^CREATE\s+PROC\s")


def norm_key(line: str) -> str:
    """Comparison key for one line.

    Ignores indentation, trailing space and the CREATE-header storage artifact.
    Two lines with the same key are the same line as far as SQL is concerned.
    """
    k = line.replace("\t", " ")
    k = _WS.sub(" ", k).strip()
    k = k.replace("CREATE OR ALTER ", "CREATE ")
    k = _CREATE_PROC.sub("CREATE PROCEDURE ", k)
    return k


def content_lines(text: str) -> list[tuple[int, str]]:
    """[(1-based line number, raw line)] for every line that contains anything."""
    return [(i + 1, ln) for i, ln in enumerate(text.split("\n")) if ln.strip()]


_MODULE_KIND = r"(?:PROCEDURE|PROC|FUNCTION|VIEW|TRIGGER)"
_HEADER_STARTS = re.compile(r"^\s*(?:CREATE|ALTER)\b", re.IGNORECASE)
_HEADER_COMPLETE = re.compile(
    r"^\s*(?:CREATE|ALTER)\s+(?:OR\s+ALTER\s+)?" + _MODULE_KIND +
    r"\s+(?:\[?\w+\]?\s*\.\s*)?\[?\w+\]?",
    re.IGNORECASE,
)
_COMMENT_ONLY = re.compile(r"^\s*(?:--|/\*|\*/|\*)")
_MAX_HEADER_LINES = 8


def _is_noise_preamble(line: str) -> bool:
    """Deployment residue that sits above the header: banner rules and comments.

    A release script's separator line frequently ends up inside the stored
    definition of whatever object followed it. It is not part of the module.
    """
    s = line.strip()
    return bool(s) and (set(s) <= set("-=/*") or bool(_COMMENT_ONLY.match(line)))


def canonical_lines(text: str) -> tuple[list[tuple[int, str, str]], int]:
    """Put both sides into the same shape before they are compared.

    Returns ([(original line number, text to display, comparison key)], preamble).

    SQL Server does not store a module the way it was written. The same
    procedure can come back with its header split over several lines with blanks
    between them, and with a leftover separator rule above it. Comparing that
    line-by-line against a release file - where the header is one line - reports
    a difference that does not exist.

    So the header is merged into a single entry on both sides, and deployment
    residue above it is set aside and counted. The line number reported is where
    the statement actually starts, so it still points into the real text.
    """
    raw = content_lines(text)
    start = next((i for i, (_, ln) in enumerate(raw) if _HEADER_STARTS.match(ln)), None)
    if start is None:
        return [(n, ln, norm_key(ln)) for n, ln in raw], 0

    out: list[tuple[int, str, str]] = []
    preamble = 0
    for n, ln in raw[:start]:
        if _is_noise_preamble(ln):
            preamble += 1
        else:
            out.append((n, ln, norm_key(ln)))

    j = start
    merged = raw[start][1].strip()
    while not _HEADER_COMPLETE.match(merged) and j + 1 < len(raw) \
            and (j - start) < _MAX_HEADER_LINES:
        j += 1
        merged = f"{merged} {raw[j][1].strip()}"

    if _HEADER_COMPLETE.match(merged):
        out.append((raw[start][0], merged, norm_key(merged)))
    else:
        # No recognisable module header - leave the lines exactly as they are.
        j = start - 1

    for n, ln in raw[j + 1:]:
        out.append((n, ln, norm_key(ln)))
    return out, preamble


def logical_text(text: str) -> str:
    """Whitespace- and header-insensitive form of a whole object, for hashing."""
    return "\n".join(k for k in (norm_key(ln) for ln in text.split("\n")) if k)


def logical_sha(text: str) -> str:
    """SHA-256 of `logical_text`, UTF-16LE encoded to match SQL Server's nvarchar."""
    return hashlib.sha256(logical_text(text).encode("utf-16-le")).hexdigest().upper()


@dataclass
class DiffStats:
    same: int = 0
    ws: int = 0
    changed: int = 0
    added: int = 0
    removed: int = 0
    left_total: int = 0
    right_total: int = 0
    left_content: int = 0
    right_content: int = 0
    left_blank: int = 0
    right_blank: int = 0
    left_preamble: int = 0
    right_preamble: int = 0

    @property
    def is_identical(self) -> bool:
        return self.changed == 0 and self.added == 0 and self.removed == 0


def build_diff(left_text: str, right_text: str) -> tuple[list[tuple], DiffStats]:
    """Align two object bodies line by line.

    Returns (rows, stats). Each row is
    (kind, left_lineno, left_text, right_lineno, right_text) where kind is one of
    same | ws | chg | add | del. Every content line of both sides appears exactly
    once, so the rendered diff is complete.
    """
    Lc, l_pre = canonical_lines(left_text)
    Rc, r_pre = canonical_lines(right_text)
    L = [(n, t) for n, t, _ in Lc]
    R = [(n, t) for n, t, _ in Rc]
    lk = [k for _, _, k in Lc]
    rk = [k for _, _, k in Rc]
    sm = difflib.SequenceMatcher(None, lk, rk, autojunk=False)

    rows: list[tuple] = []
    st = DiffStats()
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for o in range(i2 - i1):
                ln, lt = L[i1 + o]
                rn, rt = R[j1 + o]
                if lt.rstrip() == rt.rstrip():
                    rows.append(("same", ln, lt, rn, rt))
                    st.same += 1
                else:
                    rows.append(("ws", ln, lt, rn, rt))
                    st.ws += 1
        elif tag == "replace":
            for o in range(max(i2 - i1, j2 - j1)):
                ln, lt = L[i1 + o] if i1 + o < i2 else (None, None)
                rn, rt = R[j1 + o] if j1 + o < j2 else (None, None)
                rows.append(("chg", ln, lt, rn, rt))
                if lt is None:
                    st.added += 1
                elif rt is None:
                    st.removed += 1
                else:
                    st.changed += 1
        elif tag == "delete":
            for o in range(i1, i2):
                rows.append(("del", L[o][0], L[o][1], None, None))
                st.removed += 1
        elif tag == "insert":
            for o in range(j1, j2):
                rows.append(("add", None, None, R[o][0], R[o][1]))
                st.added += 1

    st.left_total = len(left_text.split("\n"))
    st.right_total = len(right_text.split("\n"))
    st.left_content, st.right_content = len(L), len(R)
    st.left_preamble, st.right_preamble = l_pre, r_pre
    st.left_blank = st.left_total - len(content_lines(left_text))
    st.right_blank = st.right_total - len(content_lines(right_text))
    return rows, st


# --------------------------------------------------------------------------- #
# Locked visual identity
# --------------------------------------------------------------------------- #

CSS = r"""
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bitter:wght@500;600;700&family=Source+Sans+3:ital,wght@0,400;0,600;0,700;1,400&family=JetBrains+Mono:wght@400;600&display=swap">
<style>
:root{
  --ground:#F6F8F9; --panel:#FFFFFF; --panel-2:#EEF2F3; --ink:#0F1519; --ink-2:#44545C;
  --ink-3:#6B7C85; --rule:#D7E0E3; --rule-2:#C2CFD4; --accent:#0E7C86; --accent-ink:#0A5A61;
  --new:#5B4BB5; --chg:#B45309; --ok:#2E7D4F; --pend:#5A6B75; --crit:#B3261E;
  --add-bg:#E4F2E8; --add-ink:#1C5C38; --del-bg:#FBE7E6; --del-ink:#8C2019;
  --chg-bg:#FDF0DC; --ws-bg:#F0F3F4;
  --shadow:0 1px 2px rgba(15,21,25,.06),0 8px 24px -12px rgba(15,21,25,.18);
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --ground:#0D1316; --panel:#141C20; --panel-2:#1B252A; --ink:#E7EDEF; --ink-2:#A9BAC1;
  --ink-3:#7C8F97; --rule:#263238; --rule-2:#33454C; --accent:#3FBCC6; --accent-ink:#6FD5DD;
  --new:#A89BF0; --chg:#E9A23B; --ok:#5FC189; --pend:#8FA2AB; --crit:#F08A80;
  --add-bg:#12301F; --add-ink:#7FD9A2; --del-bg:#331715; --del-ink:#F0938A;
  --chg-bg:#33260F; --ws-bg:#182126;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 28px -14px rgba(0,0,0,.7);
}}
:root[data-theme="dark"]{
  --ground:#0D1316; --panel:#141C20; --panel-2:#1B252A; --ink:#E7EDEF; --ink-2:#A9BAC1;
  --ink-3:#7C8F97; --rule:#263238; --rule-2:#33454C; --accent:#3FBCC6; --accent-ink:#6FD5DD;
  --new:#A89BF0; --chg:#E9A23B; --ok:#5FC189; --pend:#8FA2AB; --crit:#F08A80;
  --add-bg:#12301F; --add-ink:#7FD9A2; --del-bg:#331715; --del-ink:#F0938A;
  --chg-bg:#33260F; --ws-bg:#182126;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 28px -14px rgba(0,0,0,.7);
}
*{box-sizing:border-box}
body{background:var(--ground);color:var(--ink);
  font-family:"Source Sans 3",ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif;
  font-size:16px;line-height:1.55;margin:0;padding-block:0;padding-left:20px;padding-right:20px;
  -webkit-text-size-adjust:100%}
.wrap{max-width:1500px;margin:0 auto;display:grid;grid-template-columns:250px minmax(0,1fr);
  gap:36px;align-items:start}
@media (max-width:980px){.wrap{grid-template-columns:1fr;gap:20px}}
h1,h2,h3,h4{font-family:Bitter,Georgia,serif;text-wrap:balance;margin:0}
h1{font-size:clamp(1.7rem,4.2vw,2.5rem);font-weight:700;letter-spacing:-.015em;line-height:1.15}
h2{font-size:1.45rem;font-weight:600;letter-spacing:-.01em}
h3{font-size:1.06rem;font-weight:600}
code,kbd,.mono{font-family:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace}
a{color:var(--accent-ink)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:3px}
.rail{position:sticky;top:0;max-height:100vh;overflow-y:auto;padding-block:26px 30px}
@media (max-width:980px){.rail{position:static;max-height:none;padding-block:12px 0}}
.rail h2{font-size:.72rem;letter-spacing:.14em;text-transform:uppercase;color:var(--ink-3);
  font-family:"Source Sans 3",sans-serif;font-weight:700;margin:22px 0 8px}
.rail a{display:flex;gap:8px;align-items:baseline;text-decoration:none;color:var(--ink-2);
  padding:3px 0;font-size:.88rem;line-height:1.35}
.rail a:hover{color:var(--accent-ink)}
.rail a .n{font-family:"JetBrains Mono",monospace;font-size:.76rem;color:var(--ink-3);
  min-width:34px;font-variant-numeric:tabular-nums}
.rail .top{font-family:Bitter,serif;font-weight:700;font-size:1rem;color:var(--ink);
  display:block;padding:2px 0 10px;text-decoration:none;border-bottom:1px solid var(--rule)}
main{padding-block:26px 90px;min-width:0}
.mast{border-bottom:2px solid var(--ink);padding-bottom:18px;margin-bottom:26px}
.eyebrow{font-size:.72rem;letter-spacing:.16em;text-transform:uppercase;color:var(--accent-ink);
  font-weight:700;margin-bottom:10px}
.sub{color:var(--ink-2);font-size:1.03rem;max-width:66ch;margin:10px 0 0}
.meta{display:flex;flex-wrap:wrap;gap:6px 22px;margin-top:16px;font-size:.84rem;color:var(--ink-3)}
.meta b{color:var(--ink-2);font-weight:600}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:1px;
  background:var(--rule);border:1px solid var(--rule);margin:26px 0}
.tile{background:var(--panel);padding:16px 18px}
.tile .v{font-family:Bitter,serif;font-size:2rem;font-weight:700;line-height:1;
  font-variant-numeric:tabular-nums}
.tile .k{font-size:.78rem;color:var(--ink-3);margin-top:7px;letter-spacing:.02em}
.tile.new .v{color:var(--new)} .tile.chg .v{color:var(--chg)}
.tile.ok .v{color:var(--ok)} .tile.pend .v{color:var(--pend)} .tile.crit .v{color:var(--crit)}
.lede{background:var(--panel);border-left:3px solid var(--accent);padding:18px 20px;margin:24px 0}
.lede p{margin:0 0 10px;max-width:70ch} .lede p:last-child{margin-bottom:0}
section{margin:44px 0 0;scroll-margin-top:12px}
.shead{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:baseline;
  border-bottom:1px solid var(--rule-2);padding-bottom:9px;margin-bottom:8px}
.shead .num{font-family:"JetBrains Mono",monospace;font-size:.82rem;color:var(--accent-ink);
  font-weight:600}
.note{color:var(--ink-2);font-size:.93rem;margin:10px 0 16px;max-width:72ch}
.tw{overflow-x:auto;border:1px solid var(--rule);background:var(--panel)}
table{border-collapse:collapse;width:100%;font-size:.88rem}
th{text-align:left;font-weight:700;font-size:.72rem;letter-spacing:.09em;text-transform:uppercase;
  color:var(--ink-3);padding:10px 12px;border-bottom:1px solid var(--rule-2);
  background:var(--panel-2);white-space:nowrap;position:sticky;top:0;z-index:1}
td{padding:9px 12px;border-bottom:1px solid var(--rule);vertical-align:top}
tbody tr:last-child td{border-bottom:0}
td.id{font-family:"JetBrains Mono",monospace;font-size:.8rem;color:var(--ink-3);white-space:nowrap;
  font-variant-numeric:tabular-nums}
td.obj{font-family:"JetBrains Mono",monospace;font-size:.82rem;word-break:break-word;min-width:200px}
td.num{text-align:right;font-variant-numeric:tabular-nums;font-family:"JetBrains Mono",monospace;
  font-size:.82rem;white-space:nowrap}
.chip{display:inline-block;font-size:.69rem;font-weight:700;letter-spacing:.07em;
  text-transform:uppercase;padding:3px 8px;border:1px solid currentColor;border-radius:2px;
  white-space:nowrap;line-height:1.3}
.chip.new{color:var(--new)} .chip.chg{color:var(--chg)} .chip.ok{color:var(--ok)}
.chip.pend{color:var(--pend)} .chip.crit{color:var(--crit)}
.obj{border:1px solid var(--rule);background:var(--panel);margin:18px 0;box-shadow:var(--shadow)}
.obj>summary{list-style:none;cursor:pointer;padding:13px 16px;display:flex;flex-wrap:wrap;
  gap:8px 14px;align-items:center;background:var(--panel-2);border-bottom:1px solid var(--rule)}
.obj>summary::-webkit-details-marker{display:none}
.obj>summary::before{content:"\25B8";color:var(--ink-3);font-size:.8rem;transition:transform .15s}
.obj[open]>summary::before{transform:rotate(90deg)}
@media (prefers-reduced-motion:reduce){.obj>summary::before{transition:none}}
.obj .nm{font-family:"JetBrains Mono",monospace;font-size:.86rem;font-weight:600;
  word-break:break-word;flex:1 1 260px}
.obj .bid{font-family:"JetBrains Mono",monospace;font-size:.76rem;color:var(--ink-3);
  font-variant-numeric:tabular-nums}
.tally{display:flex;gap:10px;font-size:.76rem;font-family:"JetBrains Mono",monospace;
  color:var(--ink-3);font-variant-numeric:tabular-nums;flex-wrap:wrap}
.tally .a{color:var(--add-ink)} .tally .c{color:var(--chg)} .tally .d{color:var(--del-ink)}
.legend{font-size:.8rem;color:var(--ink-3);padding:9px 16px;border-bottom:1px solid var(--rule);
  background:var(--panel)}
.dw{overflow-x:auto}
table.diff{border-collapse:collapse;width:100%;font-family:"JetBrains Mono",monospace;
  font-size:12.5px;line-height:1.5;table-layout:fixed;min-width:760px}
table.diff col.ln{width:52px} table.diff col.tx{width:calc(50% - 52px)}
table.diff th{font-family:"Source Sans 3",sans-serif;position:static}
table.diff td{padding:1px 8px;border-bottom:0;white-space:pre-wrap;word-break:break-word;
  vertical-align:top;border-right:1px solid var(--rule)}
table.diff td.l{text-align:right;color:var(--ink-3);background:var(--panel-2);user-select:none;
  font-size:11px;font-variant-numeric:tabular-nums;padding:1px 6px}
tr.r-same td.t{background:var(--panel)}
tr.r-ws   td.t{background:var(--ws-bg)}
tr.r-chg  td.tl{background:var(--del-bg);color:var(--del-ink)}
tr.r-chg  td.tr{background:var(--add-bg);color:var(--add-ink)}
tr.r-add  td.tr{background:var(--add-bg);color:var(--add-ink)}
tr.r-add  td.tl{background:var(--panel-2)}
tr.r-del  td.tl{background:var(--del-bg);color:var(--del-ink)}
tr.r-del  td.tr{background:var(--panel-2)}
.hidesame tr.r-same,.hidesame tr.r-ws{display:none}
.ctl{display:flex;gap:16px;align-items:center;flex-wrap:wrap;padding:9px 16px;
  border-bottom:1px solid var(--rule);font-size:.84rem;background:var(--panel)}
.ctl label{display:flex;gap:7px;align-items:center;cursor:pointer;color:var(--ink-2)}
.find{border:1px solid var(--rule);background:var(--panel);padding:15px 17px;margin:12px 0;
  border-left:3px solid var(--rule-2)}
.find.high{border-left-color:var(--crit)} .find.med{border-left-color:var(--chg)}
.find.low{border-left-color:var(--pend)}
.find h3{margin:0 0 6px;font-size:1rem}
.find .where{font-family:"JetBrains Mono",monospace;font-size:.75rem;color:var(--ink-3);
  margin-bottom:5px}
.find p{margin:0;color:var(--ink-2);font-size:.93rem;max-width:74ch}
details.raw{margin:10px 0 0;border-top:1px solid var(--rule)}
details.raw summary{cursor:pointer;font-size:.8rem;color:var(--ink-3);padding:8px 16px}
details.raw pre,pre.body{margin:0;padding:12px 16px;overflow-x:auto;background:var(--panel-2);
  font-family:"JetBrains Mono",monospace;font-size:12px;line-height:1.5;white-space:pre}
details.raw pre{border-top:1px solid var(--rule)}
footer{margin-top:60px;padding-top:18px;border-top:1px solid var(--rule-2);color:var(--ink-3);
  font-size:.83rem}
footer b{color:var(--ink-2)}
</style>
"""

STANDALONE_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  html{color-scheme:light dark}
  body{margin:0;font:14px system-ui,-apple-system,"Segoe UI",sans-serif}
  img{max-width:100%}
  [hidden]{display:none!important}
</style>
"""

CHIP = {
    "changed": ("chg", "Changed"),
    "new": ("new", "New"),
    "identical": ("ok", "Identical"),
    "missing": ("crit", "Missing"),
    "pending": ("pend", "Not applied"),
    "applied": ("ok", "Applied"),
    "none": ("ok", "No change"),
}


# --------------------------------------------------------------------------- #
# Page builder
# --------------------------------------------------------------------------- #


@dataclass
class _Section:
    sid: str
    num: str
    title: str
    note: str = ""
    rail_label: str = ""
    rail_num: str = ""
    html: list[str] = field(default_factory=list)


class Report:
    """Builds one locked-format comparison page.

    `left_label` / `right_label` name the two sides everywhere they appear, so
    the same format serves 'Production now' vs 'Release 26.09.02.00' just as
    well as 'stg' vs 'prod' or 'file A' vs 'file B'.
    """

    def __init__(self, title, eyebrow, headline, standfirst,
                 left_label, right_label, meta=None):
        self.title = title
        self.eyebrow = eyebrow
        self.headline = headline
        self.standfirst = standfirst
        self.left_label = left_label
        self.right_label = right_label
        self.meta = meta or []
        self._sections: list[_Section] = []
        self._rail_groups: list[tuple[str, list[tuple[str, str, str]]]] = []
        self._intro: list[str] = []
        self._footer = ""

    # -- intro ------------------------------------------------------------- #

    def lede(self, paragraphs: list[str], sid="summary", title="What this comparison found"):
        s = _Section(sid, "", title, rail_label=title)
        s.html.append('<div class="lede">')
        for p in paragraphs:
            s.html.append(f"<p>{p}</p>")
        s.html.append("</div>")
        self._sections.append(s)
        return self

    def tiles(self, items: list[tuple[str, str, str]]):
        """items: (css class, big value, label). Appends to the last section."""
        s = self._sections[-1]
        s.html.append('<div class="tiles">')
        for cls, val, lab in items:
            s.html.append(
                f'<div class="tile {cls}"><div class="v">{val}</div>'
                f'<div class="k">{lab}</div></div>'
            )
        s.html.append("</div>")
        return self

    def note(self, text: str):
        self._sections[-1].html.append(f'<p class="note">{text}</p>')
        return self

    def findings(self, items: list[tuple[str, str, str, str]],
                 sid="findings", title="Things to check first", note=""):
        """items: (severity high|med|low, where, title, body)."""
        s = _Section(sid, "", title, rail_label=title)
        if note:
            s.html.append(f'<p class="note">{note}</p>')
        lab = {"high": "Check first", "med": "Worth knowing", "low": "Minor"}
        chip = {"high": "crit", "med": "chg", "low": "pend"}
        for sev, where, ttl, body in items:
            s.html.append(
                f'<div class="find {sev}"><div class="where">{E(where)} &nbsp;&middot;&nbsp; '
                f'<span class="chip {chip[sev]}">{lab[sev]}</span></div>'
                f"<h3>{E(ttl)}</h3><p>{E(body)}</p></div>"
            )
        self._sections.append(s)
        return self

    def reading_guide(self, extra: str = "", sid="how"):
        s = _Section(sid, "", "How to read the diffs", rail_label="How to read the diffs")
        s.html.append(
            '<div class="lede">'
            f"<p>Each object below is shown side by side: <b>left is {E(self.left_label)}</b>, "
            f"<b>right is {E(self.right_label)}</b>. Line numbers on each side are the real "
            "line numbers within that object.</p>"
            '<p>Colour tells you what happens to each line: '
            '<span class="chip ok">green, right side</span> a line that only the right side has, '
            '<span class="chip crit">red, left side</span> a line only the left side has, '
            "and a red/green pair means the line is rewritten. Grey rows are unchanged; "
            "slightly shaded grey rows are identical apart from spacing.</p>"
            "<p>Blank lines are left out of the comparison. SQL Server stores them "
            "inconsistently and they carry no meaning. Every line that contains anything at "
            "all is shown, and each object states its blank-line counts so the totals "
            "reconcile.</p>"
            + (f"<p>{extra}</p>" if extra else "")
            + "</div>"
        )
        self._sections.append(s)
        return self

    # -- generic sections --------------------------------------------------- #

    def section(self, sid, title, num="", note="", rail_label=None, rail_num=""):
        s = _Section(sid, num, title, note, rail_label or title, rail_num)
        if note:
            s.html.append(f'<p class="note">{note}</p>')
        self._sections.append(s)
        return self

    def table(self, headers: list[str], rows: list[list[str]]):
        """rows contain raw HTML cells - use `cell()` helpers to build them."""
        s = self._sections[-1]
        s.html.append('<div class="tw"><table><thead><tr>')
        for h in headers:
            s.html.append(f"<th>{h}</th>")
        s.html.append("</tr></thead><tbody>")
        for r in rows:
            s.html.append("<tr>" + "".join(r) + "</tr>")
        s.html.append("</tbody></table></div>")
        return self

    def raw_block(self, label: str, text: str):
        if not text:
            return self
        n = len(text.split("\n"))
        self._sections[-1].html.append(
            f'<details class="raw"><summary>{E(label)} &mdash; {n} lines</summary>'
            f"<pre>{E(text)}</pre></details>"
        )
        return self

    def html_block(self, raw_html: str):
        self._sections[-1].html.append(raw_html)
        return self

    # -- object diffs ------------------------------------------------------- #

    def object_diff(self, anchor, name, rows, stats: DiffStats,
                    bid="", verdict="changed", open_first=False, raw_sql=""):
        s = self._sections[-1]
        cls, lab = CHIP[verdict]
        chg = sum(1 for t, l, lt, r, rt in rows if t == "chg" and lt is not None and rt is not None)
        add = sum(1 for t, l, lt, r, rt in rows if t == "add" or (t == "chg" and lt is None))
        rem = sum(1 for t, l, lt, r, rt in rows if t == "del" or (t == "chg" and rt is None))
        idx = len([1 for x in s.html if 'class="obj"' in x])
        s.html.append(f'<details class="obj" id="{E(anchor)}"{" open" if open_first else ""}>')
        s.html.append(
            f'<summary><span class="bid">{E(bid)}</span><span class="nm">{E(name)}</span>'
            f'<span class="chip {cls}">{lab}</span>'
            f'<span class="tally"><span class="c">{chg} changed</span>'
            f'<span class="a">+{add}</span><span class="d">-{rem}</span>'
            f'<span>{stats.left_content} &rarr; {stats.right_content} lines</span></span></summary>'
        )
        s.html.append('<div class="objbody">')
        pre = ""
        if stats.left_preamble or stats.right_preamble:
            pre = (f" Deployment residue above the header (separator rules, banner "
                   f"comments) set aside: {stats.left_preamble} on the left, "
                   f"{stats.right_preamble} on the right.")
        s.html.append(
            f'<div class="legend">Both sides are put into the same shape before '
            f"comparing, so formatting alone never shows as a change. Blank lines "
            f"excluded: {stats.left_blank} on the left, {stats.right_blank} on the "
            f"right.{pre} Content lines compared: {stats.left_content} vs "
            f"{stats.right_content}.</div>"
        )
        s.html.append(
            f'<div class="ctl"><label><input type="checkbox" id="hs-{E(anchor)}-{idx}" '
            "onchange=\"this.closest('.objbody').querySelector('.dw')"
            ".classList.toggle('hidesame',this.checked)\"> Hide unchanged lines</label>"
            f'<span style="color:var(--ink-3)">{stats.same} identical, '
            f"{stats.ws} spacing-only</span></div>"
        )
        s.html.append(
            '<div class="dw"><table class="diff">'
            '<colgroup><col class="ln"><col class="tx"><col class="ln"><col class="tx"></colgroup>'
            f'<thead><tr><th colspan="2">{E(self.left_label)}</th>'
            f'<th colspan="2">{E(self.right_label)}</th></tr></thead><tbody>'
        )
        kindcls = {"same": "r-same", "ws": "r-ws", "chg": "r-chg", "add": "r-add", "del": "r-del"}
        for kind, ln, lt, rn, rt in rows:
            s.html.append(
                f'<tr class="{kindcls[kind]}">'
                f'<td class="l">{ln if ln else ""}</td>'
                f'<td class="t tl">{E(lt) if lt is not None else ""}</td>'
                f'<td class="l">{rn if rn else ""}</td>'
                f'<td class="t tr">{E(rt) if rt is not None else ""}</td></tr>'
            )
        s.html.append("</tbody></table></div>")
        if raw_sql:
            n = len(raw_sql.split("\n"))
            s.html.append(
                f'<details class="raw"><summary>Show the full script for this object '
                f"&mdash; {n} lines</summary><pre>{E(raw_sql)}</pre></details>"
            )
        s.html.append("</div></details>")
        return self

    def object_body(self, anchor, name, text, bid="", verdict="new", legend=""):
        """A one-sided object - present on only one side, so nothing to align."""
        s = self._sections[-1]
        cls, lab = CHIP[verdict]
        n = len([ln for ln in text.split("\n") if ln.strip()])
        s.html.append(
            f'<details class="obj" id="{E(anchor)}"><summary><span class="bid">{E(bid)}</span>'
            f'<span class="nm">{E(name)}</span><span class="chip {cls}">{lab}</span>'
            f'<span class="tally"><span class="a">{n} lines</span></span></summary>'
            f'<div class="objbody"><div class="legend">'
            f'{E(legend) if legend else f"Present on one side only, with {n} content lines."}'
            f'</div><pre class="body">{E(text)}</pre></div></details>'
        )
        return self

    # -- rail / method / render --------------------------------------------- #

    def rail_group(self, heading: str, links: list[tuple[str, str, str]]):
        """links: (href, small number, label)."""
        self._rail_groups.append((heading, links))
        return self

    def method(self, paragraphs: list[str], checks: list[tuple[str, str]],
               sid="method", title="How this was verified"):
        s = _Section(sid, "", title, rail_label="How this was verified")
        s.html.append('<div class="lede">')
        for p in paragraphs:
            s.html.append(f"<p>{p}</p>")
        s.html.append("</div>")
        s.html.append('<div class="tw"><table><thead><tr><th>Check</th><th>Result</th>'
                      "</tr></thead><tbody>")
        for k, v in checks:
            s.html.append(f"<tr><td>{k}</td><td>{v}</td></tr>")
        s.html.append("</tbody></table></div>")
        self._sections.append(s)
        return self

    def footer(self, text: str):
        self._footer = text
        return self

    def render(self, standalone: bool = False) -> str:
        out: list[str] = []
        if standalone:
            out.append(STANDALONE_HEAD)
        out.append(f"<title>{E(self.title)}</title>")
        out.append(CSS)
        if standalone:
            out.append("</head>\n<body>")
        out.append('<div class="wrap">')

        # index rail
        out.append('<nav class="rail" aria-label="Report index">')
        out.append(f'<a class="top" href="#top">{E(self.headline)}</a>')
        auto = [(f"#{s.sid}", s.rail_num, s.rail_label) for s in self._sections if s.rail_label]
        if self._rail_groups:
            for heading, links in self._rail_groups:
                out.append(f"<h2>{E(heading)}</h2>")
                for href, num, lab in links:
                    out.append(f'<a href="{href}"><span class="n">{E(num)}</span>{E(lab)}</a>')
        else:
            out.append("<h2>Contents</h2>")
            for href, num, lab in auto:
                out.append(f'<a href="{href}"><span class="n">{E(num)}</span>{E(lab)}</a>')
        out.append("</nav>")

        # masthead
        out.append('<main id="top"><header class="mast">')
        out.append(f'<div class="eyebrow">{E(self.eyebrow)}</div>')
        out.append(f"<h1>{E(self.headline)}</h1>")
        out.append(f'<p class="sub">{self.standfirst}</p>')
        if self.meta:
            out.append('<div class="meta">')
            for k, v in self.meta:
                out.append(f"<span><b>{E(k)}</b> {E(v)}</span>")
            out.append("</div>")
        out.append("</header>")

        for s in self._sections:
            out.append(f'<section id="{E(s.sid)}"><div class="shead">')
            if s.num:
                out.append(f'<span class="num">{E(s.num)}</span>')
            out.append(f"<h2>{E(s.title)}</h2></div>")
            out.extend(s.html)
            out.append("</section>")

        if self._footer:
            out.append(f"<footer>{self._footer}</footer>")
        out.append("</main></div>")
        if standalone:
            out.append("</body>\n</html>")
        return "\n".join(out)
