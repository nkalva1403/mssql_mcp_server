---
name: sql-compare
description: Compare a consolidated SQL release file against a live SQL Server environment (procedures, functions, views) and produce a clickable HTML drift report with the actual changed SQL text. STRICTLY READ-ONLY. Use this skill when the user says "compare", "/compare", "/sql-compare", "drift", "release diff", "compare X.sql with stg/prod/dev", "check if file matches DB", "what would deploy", or pastes a path to a consolidated release SQL file and asks what changed. Also use when verifying whether staging has hotfixes the file is missing.
---

# SQL release compare

When the user asks to compare a SQL release file against a database,
drive the workflow below. Strictly read-only. Token-efficient by design:
the heavy data stays on disk, the chat only sees a tiny summary and a
link to the HTML report.

## Inputs to resolve from the user

- **File** — a path to a `.sql` file (e.g. `Consolidated_DB_Scripts_*.sql`).
  If the user pasted a path, use it; if not, ask once.
- **Target environment** — name from `mcp__mssql__list_environments`
  (`stg`, `prod`, `dev`, ...). If not stated, **ask one short question**
  with the available env names as options. Do not guess.
- **Schema** — defaults to `dbo`. If the file uses another schema, use that.
- **Output directory** — pick a clean directory inside the project at
  `<repo>/build/drift/<yyyymmdd-hhmm>/` or honor user override.

## Hard rules (must enforce)

1. **READ-ONLY.** Never run DDL or DML. Only `mcp__mssql__execute_query`
   with `SELECT`, `mcp__mssql__get_object_definition`, `describe_table`,
   `list_*`. Refuse anything else, even if asked.
2. **Only objects in the file.** Don't enumerate or report on
   staging-only objects. Extract the inventory from the file.
3. **Token budget.** Don't paste proc bodies, audit history, or full
   diffs into the chat. Everything lives on disk. The final assistant
   message must fit in one screen: summary table + 1–3 critical findings
   + path to the HTML.
4. **Verify env before connecting.** Always call `current_environment`
   and `server_info` after `switch_environment`; abort if `mode` is
   not `read_only`.

## Steps

### 1. Switch and verify env

```
mcp__mssql__switch_environment(name=<env>, database=<db?>)
mcp__mssql__current_environment()
mcp__mssql__server_info()
```

Confirm `"mode":"read_only"`. If it isn't, stop and tell the user.

### 2. Extract object inventory from the file

Use the Grep tool against the file path:

```
pattern: ^\s*(?:CREATE\s+(?:OR\s+ALTER\s+)?|ALTER\s+)(?:PROCEDURE|PROC|FUNCTION|VIEW)\s+
output_mode: content
-n: true
-i: true
```

Also catch split-line `CREATE\n OR ALTER PROCEDURE ...` — grep for
`^\s*(?:OR\s+ALTER\s+)?(?:PROCEDURE|PROC|FUNCTION|VIEW)\s+`. Skip
`CREATE TABLE` inside proc bodies (those are `#temp` tables).

De-duplicate names (some appear twice in release scripts — both apply
to the same target object). Build a final list of unique names.

### 3. Dump current staging definitions in one shot

Build one `execute_query` call with `format='compact'`:

```sql
SELECT o.name AS proc_name, m.definition AS def
FROM sys.sql_modules m
INNER JOIN sys.objects  o ON o.object_id = m.object_id
INNER JOIN sys.schemas  s ON s.schema_id = o.schema_id
WHERE s.name = '<schema>'
  AND o.name IN ( <comma-separated, quoted names from step 2> )
ORDER BY o.name;
```

This will exceed the inline cap and the MCP will autosave the response.
**Capture the file path from the error message** — that's the
`--staging-dump` input for the next step.

### 4. Run the compare script

```
python <repo-root>/scripts/compare_release/compare.py
  --file <user-file>
  --staging-dump <path-from-step-3>
  --out-dir <out-dir-from-input-resolution>
  --env-label <env-name>
  --open
```

The script writes:

- `<out-dir>/index.html` — summary table, clickable
- `<out-dir>/<OBJECT>.html` — per-object change blocks (only for DRIFT)
- `<out-dir>/<OBJECT>.txt` — plain-text equivalent
- `<out-dir>/report.json` — machine-readable summary

`--open` launches the index in the default browser.

### 5. Read just the summary JSON

```
Read <out-dir>/report.json
```

Use its `summary` block (total / match / differs / missing /
staging_ahead) and the `results[]` entries' `name` + `status` +
(`added`, `removed`, `changed`, `dates_only_in_staging`).

### 6. Report — short

Final assistant message must be **brief**:

- One-line headline (e.g. "21 objects compared: 6 match, 15 drift,
  2 with staging ahead").
- Tiny status table (proc name + status pill). No SQL inline.
- The 1–3 most critical findings — typically the procs flagged
  `staging ahead` (file would regress them) — mentioning each in a
  single sentence, no code.
- The HTML link: `Open <out-dir>/index.html`.
- One pointer to where the deeper detail lives (`<obj>.html`).

### 7. Forbidden in the chat output

- Don't paste the SQL of any added/removed/changed block.
- Don't quote audit-history comments.
- Don't enumerate every drifted proc with a paragraph each.
- Don't repeat what the HTML already shows.

## When the user asks follow-ups

- **"What changed in proc X?"** → Read `<out-dir>/X.txt` and summarize
  in ≤5 lines. Don't dump the whole file. If they want full detail,
  point them at the HTML page or the `.txt` file.
- **"Is staging ahead anywhere?"** → Look up
  `results[*].staging_ahead` in `report.json`.
- **"Re-run after editing the file"** → Repeat step 4 only (the dump
  from step 3 is still valid unless the env changed).

## Edge cases

- **Object in file but missing from staging** — status `MISSING`; flag
  in the summary. The deploy would create the object.
- **No drift at all** — say so and stop. Don't open a browser tab full
  of green checkmarks; just confirm.
- **Staging dump came back inline** (small enough to fit) — save the
  inline JSON manually to a file, then pass to `--staging-dump`. This
  rarely happens for real release scripts.
- **`describe_table` for tables** — out of scope for this skill (the
  script doesn't compare tables yet). If the user wants table compare,
  open a follow-up: not all CREATE TABLEs in a release file are real
  schema objects; many are local `#temp` tables.
