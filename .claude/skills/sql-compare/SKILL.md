---
name: sql-compare
description: Compare any two SQL sources - database to database, database to a consolidated release file, or file to file - and produce the locked-format HTML comparison report with full side-by-side diffs. STRICTLY READ-ONLY. Use when the user says "compare", "/compare", "/sql-compare", "drift", "release diff", "compare X.sql with stg/prod/dev", "compare prod and staging", "diff these two release files", "check if file matches DB", "what would deploy", or pastes a path to a release SQL file and asks what changed.
---

# SQL comparison reports

Every comparison this repo produces uses **one locked format**, defined in
`scripts/compare_release/report_format.py` (currently v1.0). The format was
signed off after a full production comparison and must not be re-invented per
run.

## The format is locked - do not hand-roll HTML

**Never** write report HTML, CSS or diff markup yourself, and never ask another
tool to render the comparison. Always go through the CLI:

```
python scripts/compare_release/compare.py --left <src> --right <src> --out <file.html>
```

If a report needs something the format does not have, add a component to
`report_format.py` and bump `FORMAT_VERSION` - do not special-case it in a
caller. Changing colours, fonts, section order or the diff table is a format
change, not a per-report decision.

## The three modes are one thing

A "side" is just a source. The mode is only a choice of two sources:

| Comparison | `--left` | `--right` |
|---|---|---|
| database vs database | `dump:PROD.json` | `dump:STG.json` |
| database vs release file | `dump:PROD.json` | `file:release.sql` |
| file vs file | `file:old.sql` | `file:new.sql` |

Source kinds: `file:` consolidated script (objects parsed out of it) ·
`dump:` definition dump from the MCP (a database side) · `dir:` a folder of
one-object-per-file `.sql`.

Convention: **left is the existing/current state, right is the proposed state.**
Keep it that way so "added" always means "the right side introduces this".

## Hard rules

1. **READ-ONLY.** Only `execute_query` with SELECT, `get_object_definition`,
   `describe_table`, `list_*`. Never DDL or DML, even if asked. `compare.py`
   itself never opens a connection.
2. **Verify the environment before reading.** After `switch_environment`, call
   `current_environment` and `server_info`, and confirm `mode` is `read_only`.
   Abort if it is not.
3. **Only objects in scope.** For a release comparison, take the object list
   from the file. Do not enumerate the whole database.
4. **Token discipline.** SQL bodies and diffs stay on disk. The chat gets the
   summary counts, the findings, and the link. Never paste proc bodies or diff
   hunks into the conversation.

## Steps

### 1. Resolve inputs

Ask once, only for what is genuinely missing: the two sides, and a label for
each (the labels appear as the column headings, e.g. "Production now" /
"Release 26.09.02.00"). Default output directory
`<repo>/build/compare/<yyyymmdd-hhmm>/`.

### 2. For each database side, dump definitions

Switch and verify the environment, then one query per side:

```sql
SELECT o.name AS proc_name, m.definition AS def
FROM sys.sql_modules m
JOIN sys.objects o ON o.object_id = m.object_id
JOIN sys.schemas s ON s.schema_id = o.schema_id
WHERE s.name = 'dbo'
  AND o.name IN ( <names> )
ORDER BY o.name;
```

Use `format='compact'`. The MCP autosaves oversized responses - **capture the
saved path from the message** and pass it as `dump:<path>`.

If a definition has to come back through the conversation rather than an
autosave file, pull it compressed and checksum it, so a transcription slip
cannot reach the report silently:

```sql
SELECT CAST('' AS XML).value('xs:base64Binary(sql:column("t.z"))','varchar(max)') AS b64,
       CONVERT(varchar(64), HASHBYTES('SHA2_256', CONVERT(varbinary(max), m.definition)), 2) AS sha
FROM sys.sql_modules m
JOIN sys.objects o ON o.object_id = m.object_id
CROSS APPLY (SELECT COMPRESS(m.definition) AS z) t
WHERE o.name = '<name>';
```

Decode with gzip, then confirm the SHA before writing the file. Return it in
400-character chunks - long single lines get truncated in transit.

### 3. Run the comparison

```
python scripts/compare_release/compare.py \
  --left  dump:<prod-dump.json>  --left-label  "Production now" \
  --right file:<release.sql>     --right-label "Release 26.09.02.00" \
  --title "Release 26.09.02.00 vs Production" \
  --eyebrow "Database release comparison" \
  --out <out-dir>/report.html --json <out-dir>/summary.json
```

Add `--standalone` for a file the user opens locally; omit it when the HTML
will be published as an Artifact (the host supplies the document scaffolding).
`--findings` takes a JSON list of `[severity, where, title, body]`, severity
being `high` / `med` / `low`.

### 4. Read only the summary

Read `<out-dir>/summary.json`. Use its `summary` counts and `objects[]`
verdicts. Do not read the HTML back.

### 5. Report - short

- One headline line with the counts.
- The findings that need a decision, one sentence each.
- The path or Artifact link.

Do not restate what the report already shows.

## What the format decides for you

Both sides are put into the same shape before anything is compared, so
formatting alone never reads as a change. These are already handled - do not
re-implement or "correct" them:

- **`CREATE OR ALTER` is not a difference.** SQL Server stores it as
  `CREATE    `, and may store `PROC` as `PROCEDURE`. Canonicalised on both sides.
- **A header split over several lines is not a difference.** Production often
  returns `CREATE`, blank lines, then ` PROCEDURE [dbo].[X] (` on a later line,
  while the release file has it on one. The header is merged into a single
  entry on both sides, keeping the line number where the statement really
  starts.
- **A separator rule stored above the header is not a difference.** The dashed
  rule between blocks in a release script frequently ends up inside the stored
  definition of whatever object followed it. That residue is set aside and
  counted in the object's legend.
- **Indentation and trailing whitespace are not differences.** They surface as
  a separate "spacing-only" count.
- **Blank lines are excluded** from the comparison and counted per object, so
  totals still reconcile. One real procedure came back from production with 977
  extra blank lines; without this rule its diff was unreadable.
- **Object names match case-insensitively** but display in their original
  casing.

Everything that survives those rules is a real difference. If a reviewer reports
a false positive, fix it here as a shape rule and add a test - never by editing
a generated report.

## Data and schema changes

`compare.py` compares modules (procedures, functions, views). A consolidated
release file usually also contains table/column/index DDL and data blocks, which
have no "before" text to align. For those, verify each one live with a read-only
query - does this already exist, has this already been applied - and pass the
results in as `--findings`, or add a section through `report_format.Report`.
Do not silently drop them from the report; an unmentioned block reads as
"nothing to do".

## Edge cases

- **No differences at all** - say so and stop. Do not open a report full of
  green rows.
- **Object only on the left** - the right side would not create it. Reported
  under "Only on the left".
- **`CREATE TABLE` inside a procedure body** is a `#temp` table, not a schema
  object. The file parser already ignores those.
- **Both sides empty** - the script exits 2 rather than writing an empty report.
