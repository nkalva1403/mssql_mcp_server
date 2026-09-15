# -*- coding: utf-8 -*-
"""Compare two SQL sources and render the locked-format HTML report.

One driver covers every comparison direction, because a "side" is just a source:

    database  vs database   --left dump:PROD.json   --right dump:STG.json
    database  vs file       --left dump:PROD.json   --right file:release.sql
    file      vs file       --left file:old.sql     --right file:new.sql

Read-only by construction: this script never opens a database connection.
Database sides arrive as definition dumps produced by the MCP server, which owns
the credentials and the read-only guard. Pairs with the ``sql-compare`` skill,
which drives the MCP to produce those dumps and then invokes this script.

The report format is locked in ``report_format.py`` (v1.0). Do not render HTML
here - add components there so every mode stays identical.

Usage
-----
    python compare.py --left dump:prod.json --right file:release.sql \\
                      --left-label "Production now" --right-label "Release 26.09.02.00" \\
                      --title "Release 26.09.02.00 vs Production" \\
                      --out report.html [--standalone] [--open]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import report_format as fmt  # noqa: E402
from sources import load_source  # noqa: E402

E = fmt.E


_NAME_RE = __import__("re").compile(
    r"^\s*(?:CREATE|ALTER)\s+(?:OR\s+ALTER\s+)?"
    r"(?:PROCEDURE|PROC|FUNCTION|VIEW|TRIGGER)\s+"
    r"(?:\[?\w+\]?\.)?\[?(\w+)\]?",
    __import__("re").IGNORECASE | __import__("re").MULTILINE,
)


def display_name(text: str | None, fallback: str) -> str:
    """SQL Server matches names case-insensitively but preserves the author's
    casing. Match on the upper-cased key, but show the name as it was written."""
    if text:
        m = _NAME_RE.search(text)
        if m:
            return m.group(1)
    return fallback


def classify(left: dict[str, str], right: dict[str, str]) -> list[dict]:
    """Pair up both sides by object name and decide a verdict for each."""
    names = sorted(set(left) | set(right))
    out: list[dict] = []
    for key in names:
        lt, rt = left.get(key), right.get(key)
        n = display_name(rt or lt, key)
        if lt is not None and rt is not None:
            rows, st = fmt.build_diff(lt, rt)
            verdict = "identical" if st.is_identical else "changed"
            out.append({"name": n, "verdict": verdict, "rows": rows, "stats": st,
                        "left": lt, "right": rt})
        elif rt is not None:
            out.append({"name": n, "verdict": "new", "rows": None, "stats": None,
                        "left": None, "right": rt})
        else:
            out.append({"name": n, "verdict": "missing", "rows": None, "stats": None,
                        "left": lt, "right": None})
    return out


def build_report(results, args, left_desc, right_desc, findings) -> fmt.Report:
    changed = [r for r in results if r["verdict"] == "changed"]
    identical = [r for r in results if r["verdict"] == "identical"]
    new = [r for r in results if r["verdict"] == "new"]
    missing = [r for r in results if r["verdict"] == "missing"]

    tot = {"same": 0, "ws": 0, "chg": 0, "add": 0, "rem": 0}
    for r in changed:
        s = r["stats"]
        tot["same"] += s.same; tot["ws"] += s.ws
        tot["chg"] += s.changed; tot["add"] += s.added; tot["rem"] += s.removed

    rep = fmt.Report(
        title=args.title,
        eyebrow=args.eyebrow,
        headline=args.title,
        standfirst=(
            f"Every object in both sides, compared line by line. "
            f"<b>Left</b> is {E(args.left_label)}; <b>right</b> is {E(args.right_label)}. "
            "Nothing here is sampled or estimated."
        ),
        left_label=args.left_label,
        right_label=args.right_label,
        meta=[("Left", left_desc), ("Right", right_desc),
              ("Generated", date.today().strftime("%d %b %Y")),
              ("Format", f"v{fmt.FORMAT_VERSION}")],
    )

    verb = "the right side" if not args.right_label else args.right_label
    rep.lede([
        f"<b>{len(results)} objects</b> appear in one or both sides. "
        f"<b>{len(changed)}</b> differ, <b>{len(identical)}</b> are identical, "
        f"<b>{len(new)}</b> exist only on the right, and <b>{len(missing)}</b> "
        "exist only on the left.",
        (f"Across the objects present on both sides, {verb} changes "
         f"<b>{tot['chg']:,}</b> lines, adds <b>{tot['add']:,}</b> and removes "
         f"<b>{tot['rem']:,}</b>. A further <b>{tot['same']:,}</b> lines are identical and "
         f"<b>{tot['ws']:,}</b> differ only in spacing.")
        if changed else
        "Every object present on both sides matches exactly.",
    ])
    rep.tiles([
        ("chg", str(len(changed)), "Objects that differ"),
        ("ok", str(len(identical)), "Identical"),
        ("new", str(len(new)), "Only on the right"),
        ("crit", str(len(missing)), "Only on the left"),
        ("pend", f"{tot['rem']:,}", "Lines removed"),
    ])

    if findings:
        rep.findings(findings, note="Observations that need a decision before deploying.")

    rep.reading_guide()

    # ---- index table ----
    rep.section("index", "All objects at a glance",
                note="Click a name to jump to its full comparison.")
    rows = []
    for r in results:
        cls, lab = fmt.CHIP[r["verdict"]]
        s = r["stats"]
        if s:
            l, rr = str(s.left_content), str(s.right_content)
            c, a, d = str(s.changed), str(s.added), str(s.removed)
        else:
            n = len([x for x in (r["right"] or r["left"] or "").split("\n") if x.strip()])
            l, rr = ("&mdash;", str(n)) if r["verdict"] == "new" else (str(n), "&mdash;")
            c, a, d = "&mdash;", "&mdash;", "&mdash;"
        rows.append([
            f'<td class="obj"><a href="#o-{E(r["name"])}">{E(r["name"])}</a></td>',
            f'<td><span class="chip {cls}">{lab}</span></td>',
            f'<td class="num">{l}</td>', f'<td class="num">{rr}</td>',
            f'<td class="num">{c}</td>', f'<td class="num">{a}</td>',
            f'<td class="num">{d}</td>',
        ])
    rep.table(["Object", "Status", "Left lines", "Right lines",
               "Changed", "Added", "Removed"], rows)

    # ---- per-object diffs ----
    if changed:
        rep.section("diffs", "Line-by-line comparison",
                    note=f"{len(changed)} objects that exist on both sides and differ. "
                         "Use the checkbox inside an object to hide unchanged lines.")
        for i, r in enumerate(changed):
            rep.object_diff(f"o-{r['name']}", r["name"], r["rows"], r["stats"],
                            verdict="changed", open_first=(i == 0))

    if identical and args.show_identical:
        rep.section("identical", "Identical objects",
                    note="Present on both sides with no differences.")
        for r in identical:
            rep.object_body(f"o-{r['name']}", r["name"], r["right"],
                            verdict="identical",
                            legend="Identical on both sides - shown once.")

    if new:
        rep.section("new", "Only on the right",
                    note=f"{len(new)} objects with nothing to compare against.")
        for r in new:
            rep.object_body(f"o-{r['name']}", r["name"], r["right"], verdict="new")

    if missing:
        rep.section("missing", "Only on the left",
                    note=f"{len(missing)} objects present on the left but absent on the "
                         "right. Deploying the right side would not create them.")
        for r in missing:
            rep.object_body(f"o-{r['name']}", r["name"], r["left"], verdict="missing",
                            legend="Present on the left only.")

    rep.method(
        [
            "<b>Read-only throughout.</b> This report is rendered offline from "
            "definition dumps; the comparison tool never opens a database connection.",
            "<b>Nothing was sampled.</b> Every object found in either side is listed, and "
            "every content line of every differing object is rendered.",
            "<b>Two differences are deliberately treated as identical.</b> SQL Server "
            "rewrites <span class='mono'>CREATE OR ALTER</span> as "
            "<span class='mono'>CREATE</span> when it stores a module, and it does not "
            "preserve indentation or trailing spaces. Both are storage artifacts, not "
            "edits. Blank lines are excluded for the same reason and counted per object.",
        ],
        [("Objects compared", f"{len(results)}"),
         ("Differing", f"{len(changed)}"),
         ("Identical", f"{len(identical)}"),
         ("Only on the right", f"{len(new)}"),
         ("Only on the left", f"{len(missing)}"),
         ("Report format", f"locked v{fmt.FORMAT_VERSION}"),
         ("Writes issued", "none")],
    )
    rep.footer(
        f"<b>{E(args.title)}.</b> Left: {E(left_desc)}. Right: {E(right_desc)}. "
        f"Generated {date.today().strftime('%d %B %Y')} in locked format "
        f"v{fmt.FORMAT_VERSION}. Read-only; no database was modified."
    )
    return rep


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Compare two SQL sources and render the locked-format report.")
    ap.add_argument("--left", required=True,
                    help="file:PATH | dump:PATH | dir:PATH (the 'before' side)")
    ap.add_argument("--right", required=True,
                    help="file:PATH | dump:PATH | dir:PATH (the 'after' side)")
    ap.add_argument("--left-label", default="Left side")
    ap.add_argument("--right-label", default="Right side")
    ap.add_argument("--title", default="SQL comparison")
    ap.add_argument("--eyebrow", default="Database comparison")
    ap.add_argument("--out", required=True, help="path to write the HTML report")
    ap.add_argument("--findings", help="optional JSON list of "
                                       "[severity, where, title, body] entries")
    ap.add_argument("--standalone", action="store_true",
                    help="emit a full HTML document (for opening off-line, not as an Artifact)")
    ap.add_argument("--show-identical", action="store_true",
                    help="also render the body of objects that match exactly")
    ap.add_argument("--json", help="also write a machine-readable summary here")
    ap.add_argument("--open", action="store_true", help="open the report when done")
    args = ap.parse_args()

    left, left_desc = load_source(args.left)
    right, right_desc = load_source(args.right)
    if not left and not right:
        print("both sides are empty - nothing to compare", file=sys.stderr)
        return 2

    findings = []
    if args.findings:
        findings = [tuple(x) for x in json.loads(Path(args.findings).read_text("utf-8"))]

    results = classify(left, right)
    rep = build_report(results, args, left_desc, right_desc, findings)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rep.render(standalone=args.standalone), encoding="utf-8", newline="\n")

    counts = {v: sum(1 for r in results if r["verdict"] == v)
              for v in ("changed", "identical", "new", "missing")}
    if args.json:
        Path(args.json).write_text(json.dumps({
            "format_version": fmt.FORMAT_VERSION,
            "left": left_desc, "right": right_desc,
            "summary": counts,
            "objects": [{"name": r["name"], "verdict": r["verdict"],
                         **({"changed": r["stats"].changed, "added": r["stats"].added,
                             "removed": r["stats"].removed} if r["stats"] else {})}
                        for r in results],
        }, indent=1), encoding="utf-8")

    size_mb = out.stat().st_size / 1048576
    print(f"{out}  ({size_mb:.2f} MB)")
    print(f"  {counts['changed']} differ | {counts['identical']} identical | "
          f"{counts['new']} right-only | {counts['missing']} left-only")
    if args.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
