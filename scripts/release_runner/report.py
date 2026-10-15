"""Plain-English HTML report for release runs (stdlib only).

Built from each run's plan.json (what was planned, flags, skips) and the
server's journal (what actually happened) - never from a retelling.
Readable by non-technical readers first; technical detail sits in
expandable sections."""

from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any

# status key -> (label shown to readers, css class, one-line meaning)
STATUS = {
    "succeeded": ("Done", "ok", "Ran successfully."),
    "failed": ("Failed", "bad", "SQL Server returned an error; the step's changes after the error did not happen."),
    "blocked": ("Refused", "bad", "Stopped by a safety check before reaching the database. Nothing ran."),
    "mismatch": ("Check", "bad", "The text that ran differs from the planned file. Investigate."),
    "notrun": ("Not run", "warn", "The run stopped before this step. Nothing ran."),
    "excluded": ("Not run - by decision", "warn", "You chose not to run this step at the pre-flight review."),
    "skip-already-ran": ("Skipped - already applied", "skip", "The exact same script already ran successfully on this database."),
    "skip-duplicate": ("Skipped - duplicate", "skip", "The exact same script appears earlier in this release and ran there."),
    "skip-empty": ("Nothing to run", "skip", "Only comments or blank lines."),
    "session": ("Setting", "skip", "A connection setting (e.g. QUOTED_IDENTIFIER) applied to the following steps."),
}

# Plain-language meaning of common SQL Server errors.
ERRORS = {
    102: "The script has a syntax error (a typo or missing keyword).",
    105: "A quote is not closed in the script.",
    156: "The script has a syntax error near a keyword.",
    201: "A stored procedure was called without a required parameter.",
    207: "The script refers to a column that does not exist.",
    208: "The script refers to a table/view/object that does not exist in this database.",
    213: "The INSERT gives a different number of values than columns.",
    245: "A value could not be converted to the required data type.",
    266: "A transaction was started but not finished; it was rolled back.",
    515: "A required column was left empty (NULL is not allowed).",
    544: "The script inserts an ID value without IDENTITY_INSERT switched on.",
    547: "The change conflicts with a related table (foreign key / check rule).",
    1505: "A unique index cannot be created because duplicate values exist.",
    1750: "A constraint could not be created.",
    1767: "A foreign key points at a table that does not exist.",
    1779: "The table already has a primary key.",
    1781: "The column already has a default value.",
    1913: "An index with this name already exists.",
    2601: "The row would create a duplicate value in a unique index.",
    2627: "The row would create a duplicate key (the data already exists).",
    2628: "A text value is too long for its column.",
    2705: "A column with this name already exists in the table.",
    2714: "An object with this name already exists.",
    2812: "The stored procedure being called does not exist.",
    3701: "The object to remove does not exist (or no permission).",
    3726: "The object cannot be removed because another table still references it.",
    4902: "The table to change could not be found.",
    4924: "The column change is not allowed.",
    5074: "The column cannot be changed because other objects depend on it.",
    8114: "A value could not be converted to the required data type.",
    8152: "A text value is too long for its column.",
    1205: "The step was chosen as a deadlock victim (another session was using the same data).",
    15248: "Rename failed - the object or column name was not found.",
    2759: "CREATE SCHEMA failed - the schema may already exist.",
}

CSS = """
:root{--bg:#f7f8fa;--card:#fff;--text:#1d2330;--muted:#5b6475;--line:#e3e6ec;--ok:#1a7f45;--okbg:#e6f5ec;
--bad:#b42318;--badbg:#fdecea;--warn:#a15c00;--warnbg:#fff4e0;--skip:#4a5568;--skipbg:#eef1f5;--accent:#2b59c3;--code:#f2f4f8}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1b2029;--text:#e6e9ef;--muted:#9aa3b2;--line:#2c3340;
--ok:#4cc38a;--okbg:#14301f;--bad:#ff7b72;--badbg:#3a1714;--warn:#f0b35a;--warnbg:#3a2a10;--skip:#aab4c3;--skipbg:#262c36;--accent:#7aa2ff;--code:#151920}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1200px;margin:0 auto;padding:24px 16px 64px}h1{font-size:26px;margin:0 0 4px}h2{font-size:20px;margin:36px 0 12px}
h3{font-size:16px;margin:0 0 6px}.muted{color:var(--muted)}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:10px 0}
.verdict{font-size:17px;margin:6px 0 12px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}.stat b{display:block;font-size:26px;line-height:1.1}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12.5px;font-weight:600;white-space:nowrap}
.ok{color:var(--ok);background:var(--okbg)}.bad{color:var(--bad);background:var(--badbg)}.warn{color:var(--warn);background:var(--warnbg)}.skip{color:var(--skip);background:var(--skipbg)}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top;font-size:14px}th{font-size:12.5px;text-transform:uppercase;letter-spacing:.03em;color:var(--muted)}
.tablewrap{overflow-x:auto}pre{background:var(--code);border:1px solid var(--line);border-radius:8px;padding:10px;overflow:auto;font:12.5px/1.45 ui-monospace,Consolas,monospace;max-height:420px;margin:8px 0}
.hl{background:var(--badbg);color:var(--bad);display:block}.ln{display:block}details summary{cursor:pointer;color:var(--accent)}code{font:13px ui-monospace,Consolas,monospace}
.filters{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}.filters button{border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:999px;padding:5px 12px;cursor:pointer;font:inherit;font-size:13.5px}
.filters button.on{border-color:var(--accent);color:var(--accent);font-weight:600}input[type=search]{border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:8px;padding:6px 10px;font:inherit;min-width:220px}
.flag{font-size:13px;margin:2px 0}.flag.danger{color:var(--bad)}.flag.backup,.flag.warn{color:var(--warn)}.flag.info{color:var(--muted)}
dl.kv{display:grid;grid-template-columns:max-content 1fr;gap:4px 14px;margin:8px 0}dl.kv dt{color:var(--muted)}dl.kv dd{margin:0}
@media print{.filters,input{display:none}details{open:true}}
"""

JS = """
const rows=[...document.querySelectorAll('#steps tbody tr.step')];let mode='all';
function apply(){const q=(document.getElementById('q').value||'').toLowerCase();
rows.forEach(r=>{const s=r.dataset.state;const okMode=mode==='all'||s.includes(mode);const okQ=!q||r.textContent.toLowerCase().includes(q);
const show=okMode&&okQ;r.style.display=show?'':'none';const d=r.nextElementSibling;if(d&&d.classList.contains('detail'))d.style.display=show&&d.dataset.open==='1'?'':'none';});}
document.querySelectorAll('.filters button').forEach(b=>b.onclick=()=>{document.querySelectorAll('.filters button').forEach(x=>x.classList.remove('on'));b.classList.add('on');mode=b.dataset.f;apply();});
document.getElementById('q').oninput=apply;
rows.forEach(r=>r.querySelector('.toggle').onclick=(e)=>{e.preventDefault();const d=r.nextElementSibling;d.dataset.open=d.dataset.open==='1'?'0':'1';d.style.display=d.dataset.open==='1'?'':'none';});
"""


def esc(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def _batch_text(run: dict, b: dict) -> str | None:
    f = Path(run["plan"]["root"]) / b["batch_file"]
    return f.read_text(encoding="utf-8") if f.is_file() else None


def outcome(run: dict, b: dict) -> tuple[str, dict | None, int]:
    """(status key, last journal row, attempts) for one batch in one run."""
    if b["action"] != "run":
        return b["action"], None, 0
    rows = [j for j in run["journal"] if j.get("label") == b["label"]]
    if not rows:
        return ("excluded" if b["label"] in run["excluded"] else "notrun"), None, 0
    last = rows[-1]
    st = last.get("status", "failed")
    if st != "blocked" and last.get("sql_sha256") and last["sql_sha256"] != b["sha256"]:
        st = "mismatch"
    return st, last, len(rows)


def plain_error(j: dict | None, st: str) -> str:
    if st == "mismatch":
        return "The SQL that ran is not the same as the planned file (it was changed after planning)."
    if not j:
        return ""
    if st == "blocked":
        return f"Safety check: {j.get('error_message')}"
    num = j.get("error_number")
    msg = ERRORS.get(num) if isinstance(num, int) else None
    if not msg and j.get("error_message", "").startswith("Timed out"):
        msg = "The step took too long and was cancelled."
    return msg or "SQL Server reported an error (see technical details)."


def excerpt(text: str | None, err_line: int | None) -> str:
    if not text:
        return ""
    lines = text.split("\n")
    if not err_line or err_line < 1 or err_line > len(lines):
        body = "\n".join(lines[:40]) + ("\n..." if len(lines) > 40 else "")
        return f"<pre>{esc(body)}</pre>"
    lo, hi = max(1, err_line - 4), min(len(lines), err_line + 4)
    parts = []
    for n in range(lo, hi + 1):
        line = esc(f"{n:>4}  {lines[n - 1]}")
        parts.append(f'<span class="{"hl" if n == err_line else "ln"}">{line}</span>')
    return "<pre>" + "".join(parts) + "</pre>"


def tech_details(j: dict | None, b: dict) -> str:
    if not j:
        return ""
    file_line = None
    if j.get("error_line") and not j.get("error_procedure"):
        file_line = b["start_line"] + int(j["error_line"]) - 1
    kv = [
        ("Error number", j.get("error_number")),
        ("Severity / state", f"{j.get('error_severity')} / {j.get('error_state')}" if j.get("error_severity") is not None else None),
        ("Line in this step", j.get("error_line")),
        ("Line in the file", file_line),
        ("Inside object", j.get("error_procedure")),
        ("SQL Server message", j.get("error_message")),
        ("Transaction rolled back", "Yes" if j.get("transaction_rolled_back") else None),
        ("Rows changed", ", ".join(str(x) for x in j.get("rows_affected") or []) or None),
        ("Messages", " | ".join(j.get("messages") or []) or None),
        ("Started (UTC)", (j.get("started_at") or "").replace("T", " ").replace("Z", "")),
        ("Duration", f"{(j.get('duration_ms') or 0) / 1000:.1f} s"),
    ]
    return '<dl class="kv">' + "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in kv if v not in (None, "")) + "</dl>"


def build_report(runs: list[dict], title: str) -> str:
    first = runs[0]["plan"]
    envs = [r["plan"]["environment"] for r in runs]
    # index batches by label (plans built from the same files share labels)
    labels: list[str] = []
    by_label: dict[str, dict] = {}
    for r in runs:
        for b in r["plan"]["batches"]:
            if b["label"] not in by_label:
                labels.append(b["label"])
                by_label[b["label"]] = b

    out: list[str] = [
        "<!doctype html><html lang=en><head><meta charset=utf-8>",
        '<meta name=viewport content="width=device-width,initial-scale=1">',
        f"<title>{esc(title)}</title><style>{CSS}</style></head><body><main>",
        f"<h1>{esc(title)}</h1>",
        f'<div class=muted>Release <b>{esc(first["release"])}</b> &middot; {len(first["files"])} files &middot; '
        f'{len(labels)} steps &middot; generated {datetime.now().strftime("%d %b %Y %H:%M")}</div>',
    ]

    # ---- summary per environment
    out.append("<h2>Summary</h2>")
    attention: list[tuple[dict, dict, str, dict | None, int]] = []
    for r in runs:
        p = r["plan"]
        cnt: dict[str, int] = {}
        rows_changed = 0
        for b in p["batches"]:
            st, j, n = outcome(r, b)
            cnt[st] = cnt.get(st, 0) + 1
            if j:
                rows_changed += sum(j.get("rows_affected") or [])
            if st in ("failed", "blocked", "mismatch", "notrun"):
                attention.append((r, b, st, j, n))
        to_run = sum(1 for b in p["batches"] if b["action"] == "run")
        ok = cnt.get("succeeded", 0)
        bad = cnt.get("failed", 0) + cnt.get("blocked", 0) + cnt.get("mismatch", 0)
        skipped = cnt.get("skip-already-ran", 0) + cnt.get("skip-duplicate", 0)
        if bad == 0 and cnt.get("notrun", 0) == 0:
            verdict = f'<span class="pill ok">All good</span> All {ok} steps that needed to run completed successfully.'
        else:
            verdict = (f'<span class="pill bad">Needs attention</span> {ok} of {to_run} steps completed; '
                       f'{bad} failed or were refused' + (f', {cnt.get("notrun", 0)} did not run' if cnt.get("notrun") else "") + ".")
        out.append(f'<div class=card><h3>{esc(p["environment"])} <span class=muted>({esc(p["database"])})</span></h3>'
                   f'<div class=verdict>{verdict}</div><div class=grid>'
                   f'<div class=stat><b>{to_run}</b>steps to run</div>'
                   f'<div class=stat><b style="color:var(--ok)">{ok}</b>done</div>'
                   f'<div class=stat><b style="color:var(--bad)">{bad}</b>failed / refused</div>'
                   f'<div class=stat><b style="color:var(--warn)">{cnt.get("notrun", 0) + cnt.get("excluded", 0)}</b>not run</div>'
                   f'<div class=stat><b>{skipped}</b>skipped (already applied)</div>'
                   f'<div class=stat><b>{rows_changed:,}</b>rows changed</div></div>'
                   f'<div class="muted" style="margin-top:8px">Run id <code>{esc(p["run_id"])}</code></div></div>')

    # ---- what needs attention
    out.append("<h2>What needs attention</h2>")
    if not attention:
        out.append('<div class=card>Nothing. Every step either ran successfully or was intentionally skipped.</div>')
    for r, b, st, j, n in attention:
        lab, cls, _ = STATUS[st]
        text = _batch_text(r, b)
        out.append(
            f'<div class=card><h3><span class="pill {cls}">{esc(lab)}</span> {esc(r["plan"]["environment"])} &middot; '
            f'Step {esc(b["label"])} &middot; {esc(b["summary"])}</h3>'
            f'<div class=muted>File <code>{esc(b["file"])}</code>, lines {b["start_line"]}-{b["end_line"]}'
            + (f' &middot; attempted {n} times' if n > 1 else "") + "</div>"
            f'<p><b>What happened:</b> {esc(plain_error(j, st) or STATUS[st][2])}</p>'
            + (f"<details><summary>Technical details</summary>{tech_details(j, b)}"
               f"{excerpt(text, (j or {}).get('error_line') if not (j or {}).get('error_procedure') else None)}</details>" if j or st == "mismatch" else "")
            + "</div>")

    # ---- pre-run warnings for steps that ran
    flagged = [by_label[lb] for lb in labels if by_label[lb]["flags"] and by_label[lb]["action"] == "run"]
    out.append("<h2>Warnings found before running</h2>")
    if not flagged:
        out.append("<div class=card>None.</div>")
    else:
        out.append('<div class=muted>Checks made by reading the scripts before anything ran. They are not errors - '
                   'they mark steps that deserve a second look (data changes, no re-run protection, ...). '
                   'No backup was taken by this tool.</div><div class=tablewrap><table><thead><tr><th>Step</th><th>File / lines</th>'
                   '<th>What it does</th><th>Warnings</th></tr></thead><tbody>')
        for b in flagged:
            fl = "".join(f'<div class="flag {esc(f["level"])}">{esc(f["text"])}</div>' for f in b["flags"])
            out.append(f'<tr><td>{esc(b["label"])}</td><td><code>{esc(b["file"])}</code><br><span class=muted>{b["start_line"]}-{b["end_line"]}</span></td>'
                       f'<td>{esc(b["summary"])}</td><td>{fl}</td></tr>')
        out.append("</tbody></table></div>")

    # ---- every step
    out.append("<h2>Every step, in run order</h2>")
    out.append('<div class=filters><button class=on data-f=all>All</button><button data-f=bad>Failed / refused</button>'
               '<button data-f=warn>Not run</button><button data-f=skip>Skipped</button><button data-f=ok>Done</button>'
               '<input id=q type=search placeholder="Search file, object, error..."></div>')
    head = "".join(f"<th>{esc(e)}</th>" for e in envs)
    out.append(f'<div class=tablewrap><table id=steps><thead><tr><th>Step</th><th>File / lines</th><th>Type</th>'
               f'<th>What it does</th>{head}<th>Rows changed</th><th></th></tr></thead><tbody>')
    colspan = 6 + len(envs)
    for lb in labels:
        b = by_label[lb]
        cells, states, details, rows_txt = [], [], [], []
        for r in runs:
            rb = next((x for x in r["plan"]["batches"] if x["label"] == lb), None)
            if rb is None:
                cells.append("<td class=muted>-</td>")
                continue
            st, j, n = outcome(r, rb)
            lab, cls, meaning = STATUS[st]
            states.append(cls)
            note = rb.get("action_note") or (r["excluded"].get(lb) if st == "excluded" else "")
            cells.append(f'<td><span class="pill {cls}" title="{esc(meaning)}">{esc(lab)}</span>'
                         + (f'<div class=muted style="font-size:12.5px">{esc(note)}</div>' if note else "") + "</td>")
            if j:
                rows_txt.append(f"{sum(j.get('rows_affected') or []):,}")
                details.append(f"<h3>{esc(r['plan']['environment'])}: {esc(lab)}</h3>"
                               + (f"<p>{esc(plain_error(j, st))}</p>" if st not in ("succeeded",) else "")
                               + tech_details(j, rb))
        flags = "".join(f'<div class="flag {esc(f["level"])}">&#9888; {esc(f["text"])}</div>' for f in b["flags"])
        objs = ", ".join(b["objects"][:4])
        out.append(
            f'<tr class=step data-state="{" ".join(states)}"><td><b>{esc(lb)}</b></td>'
            f'<td><code>{esc(b["file"])}</code><br><span class=muted>lines {b["start_line"]}-{b["end_line"]}</span></td>'
            f'<td>{esc(b["kind_label"])}</td><td>{esc(b["summary"])}'
            + (f'<div class=muted style="font-size:12.5px">{esc(objs)}</div>' if objs and objs not in b["summary"] else "")
            + f"{flags}</td>{''.join(cells)}<td>{esc(' / '.join(rows_txt))}</td>"
            f'<td><a href="#" class=toggle>details</a></td></tr>')
        text = None
        for r in runs:
            text = _batch_text(r, b)
            if text is not None:
                break
        sql = f"<details><summary>SQL of this step</summary><pre>{esc(text)}</pre></details>" if text else \
            '<div class=muted>SQL not stored for skipped steps.</div>'
        out.append(f'<tr class=detail data-open=0 style="display:none"><td colspan={colspan}>{"".join(details)}{sql}</td></tr>')
    out.append("</tbody></table></div>")

    # ---- files + glossary
    out.append("<h2>Files, in the order they were run</h2><div class=tablewrap><table><thead><tr><th>#</th><th>File</th>"
               "<th>Steps</th><th>Lines</th></tr></thead><tbody>")
    for f in first["files"]:
        out.append(f"<tr><td>{f['index']}</td><td><code>{esc(f['name'])}</code></td><td>{f['batches']}</td><td>{f['lines']}</td></tr>")
    out.append("</tbody></table></div>")
    out.append("<h2>How to read this report</h2><div class=card><dl class=kv>")
    for key in ("succeeded", "failed", "blocked", "notrun", "excluded", "skip-already-ran", "skip-duplicate", "skip-empty", "session", "mismatch"):
        lab, cls, meaning = STATUS[key]
        out.append(f'<dt><span class="pill {cls}">{esc(lab)}</span></dt><dd>{esc(meaning)}</dd>')
    out.append("<dt>Step</dt><dd>One block of a script between two <code>GO</code> lines. "
               "<code>F02-B005</code> = file 2, block 5.</dd>"
               "<dt>Rows changed</dt><dd>Rows added, updated or deleted, as reported by SQL Server.</dd>"
               "</dl><p class=muted>Each step ran on its own connection. When a step fails, SQL Server stops that step at the "
               "first error; statements before the error that were not inside a transaction stay applied. "
               "Results come from the database server's own run journal.</p></div>")
    out.append(f"<script>{JS}</script></main></body></html>")
    return "\n".join(out)


if __name__ == "__main__":  # pragma: no cover
    print(json.dumps(sorted(STATUS)))
