# compare_release

Compare two SQL sources and render the **locked comparison-report format**.
**Read-only** — this tool never opens a database connection.

Pairs with the [`sql-compare`](../../.claude/skills/sql-compare/SKILL.md) Claude
skill — say "compare" in Claude Code and the skill drives this script end to end,
including producing the database-side dumps through the MCP server.

## Files

| File | Role |
|---|---|
| `report_format.py` | **The locked format (v1.0).** Design tokens, CSS, page components, and the difference engine. Single source of truth. |
| `sources.py` | Loads one side of a comparison into `{OBJECT: definition}`. |
| `compare.py` | The driver: classify both sides, build the report, write HTML/JSON. |

## The three modes are one thing

A "side" is just a source, so the mode is only a choice of two sources:

```bash
# database vs database
python compare.py --left dump:prod.json --right dump:stg.json \
    --left-label prod --right-label stg --title "prod vs stg" --out report.html

# database vs consolidated release file
python compare.py --left dump:prod.json --right file:release.sql \
    --left-label "Production now" --right-label "Release 26.09.02.00" \
    --title "Release 26.09.02.00 vs Production" --out report.html

# file vs file
python compare.py --left file:old.sql --right file:new.sql \
    --left-label "Previous" --right-label "Proposed" --title "A vs B" --out report.html
```

Source kinds:

- `file:PATH` — consolidated release script; objects are parsed out of it,
  last definition of an object wins.
- `dump:PATH` — JSON definition dump from the MCP server (a database side).
  Accepts the MCP autosave envelope or a plain `{"NAME": "definition"}` map.
- `dir:PATH` — a folder of one-object-per-file `.sql` scripts.

A bare path works too: `.sql` is treated as `file:`, anything else as `dump:`.

**Convention:** left is the existing state, right is the proposed state, so
"added" always means the right side introduces it.

## Useful flags

| Flag | Effect |
|---|---|
| `--standalone` | Emit a complete HTML document for opening locally. Omit when publishing as an Artifact — the host supplies `<!doctype>/<head>/<body>`. |
| `--findings FILE.json` | A list of `[severity, where, title, body]`; severity `high`/`med`/`low`. Rendered as the "Things to check first" section. |
| `--json FILE` | Machine-readable summary — read this instead of the HTML. |
| `--show-identical` | Also render the body of objects that match exactly. |
| `--open` | Open the report when finished. |

## Same shape before comparing

SQL Server does not give a module back the way it was written. Both sides are
put into the same shape first, so formatting alone never reads as a change.
These are **not** reported as differences:

1. **`CREATE OR ALTER X` is stored as `CREATE    X`.** Both sides are
   canonicalised, as are `PROC` / `PROCEDURE`.
2. **The header can come back split over several lines.** Production often
   stores `CREATE`, blank lines, then ` PROCEDURE [dbo].[X] (` further down,
   while the release file has one line. The header is merged into a single
   entry on both sides, keeping the line number where the statement really
   starts.
3. **A separator rule stored above the header.** The dashed rule between blocks
   in a release script often ends up inside the stored definition of the object
   that followed it. Set aside and counted in the object's legend.
4. **Indentation and trailing whitespace are not preserved.** Lines matching
   apart from spacing are counted separately as "spacing-only".
5. **Blank lines are not preserved.** Excluded from the comparison and counted
   per object so the totals still reconcile. One production procedure came back
   with 977 extra blank lines; without this rule its diff was unreadable.

Rules 2 and 3 were added in v1.1 after review feedback on the 26.09.02.00
report: together they removed 8 false positives across 5 objects while leaving
every real difference intact.

Object names match case-insensitively (as SQL Server does) but display in their
original casing.

Every content line of every differing object is rendered — the report never
samples or truncates.

## Changing the format

Add a component to `report_format.py` and bump `FORMAT_VERSION`. Do not
special-case rendering in `compare.py` or in a caller: the point of the lock is
that every comparison, in every direction, produces the same document.

Tests: `pytest tests/test_compare_format.py`.
