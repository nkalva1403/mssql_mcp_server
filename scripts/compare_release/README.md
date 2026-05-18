# compare_release

Compare a consolidated SQL release file against a live SQL Server
environment. **Read-only.** Produces a clickable HTML drift report
with per-object change blocks (added / only-in-staging / changed)
and the actual SQL text on both sides.

Pairs with the [`sql-compare`](../../.claude/skills/sql-compare/SKILL.md)
Claude skill in this repo — say "compare" in Claude Code and the
skill drives this script end-to-end.

## What it does

1. **Parses the file** — finds every `CREATE [OR ALTER] PROCEDURE|FUNCTION|VIEW`
   (and `ALTER PROCEDURE`) header. For objects defined twice in the file,
   the **last** occurrence wins (release-script convention).
2. **Loads the staging dump** — reads a JSON file the running MCP server
   wrote when an `execute_query` result exceeded the inline cap. Each row
   has `name` + `definition`.
3. **Normalizes** both sides — strips `/* */` and `-- ` comments,
   `[brackets]` around identifiers, collapses whitespace, lowercases.
4. **Diffs** with `difflib.SequenceMatcher` against a *reformatted*
   version (one T-SQL keyword per line) so the change blocks are
   semantic, not whitespace noise.
5. **Renders** per-object HTML pages, an index, plain-text equivalents,
   and a machine-readable JSON summary.

## Manual usage

```powershell
python compare.py `
  --file "C:\path\to\Consolidated_DB_Scripts_*.sql" `
  --staging-dump "C:\path\to\dump-from-mcp.txt" `
  --out-dir "C:\path\to\report" `
  --env-label "stg" `
  --open
```

Producing the staging dump (one-shot, from inside Claude Code or any
MCP client):

```sql
SELECT o.name AS proc_name, m.definition AS def
FROM sys.sql_modules m
INNER JOIN sys.objects  o ON o.object_id = m.object_id
INNER JOIN sys.schemas  s ON s.schema_id = o.schema_id
WHERE s.name = 'dbo'
  AND o.name IN ( <object names from the file> )
ORDER BY o.name;
```

Call it via `mcp__mssql__execute_query` with `format='compact'`. When
the response is too large to return inline, the MCP automatically
writes it to a file under
`~/.claude/projects/.../tool-results/` and returns the path in the
error message — point `--staging-dump` at that file.

## Output layout

```
<out-dir>/
├── index.html               <- summary table, click any DRIFT row
├── report.json              <- machine-readable summary
├── <OBJECT>.html            <- per-object change blocks (one per drift)
└── <OBJECT>.txt             <- plain-text equivalent (grep-friendly)
```

Every drift block shows:

- **Context (preceding lines, identical on both sides)** — so you can
  locate the change inside the proc structure.
- **Staging (current)** / **File (replacement)** — the actual SQL on
  each side.
- **Context (following lines, identical on both sides)**.

## Status meanings

| Status         | Meaning                                                                  |
|----------------|--------------------------------------------------------------------------|
| MATCH          | File body is semantically identical to staging.                          |
| DRIFT          | File would change staging. Look at the per-object HTML for what & why.   |
| staging ahead  | Staging has audit-history dates the file doesn't — file may regress.    |
| missing        | The object is in the file but does not exist in staging.                 |

## Why the multi-pass normalize?

T-SQL formatting varies wildly between editors and the storage form in
`sys.sql_modules`. Comparing raw bytes would flag every proc as
different because of `CREATE` vs `CREATE OR ALTER`, `[brackets]`,
indentation, trailing `GO`s, etc. The aggressive `normalize()` (used
for the MATCH/DIFFER decision) collapses ALL whitespace and unifies
the prefix. The lighter `reformat()` (used only for the diff display)
preserves enough structure that the change blocks are still readable.

## Limitations

- Tables aren't currently compared (the consolidated files we've seen
  contain only `CREATE TABLE #temp` inside procs). Add a separate path
  using `INFORMATION_SCHEMA.COLUMNS` if needed.
- The "staging ahead" heuristic relies on audit-history dates in the
  proc's header comment. If a team doesn't keep that convention, you'll
  still see DRIFT but the directional hint won't be set.
- Doesn't follow `EXEC` chains — each object is compared independently.

## Token economy (when driven by Claude)

The whole point of pairing this with a skill is keeping chat context
small. The script writes everything to disk; Claude only reads back the
JSON summary, never the diffs. Per-object HTML is opened in the user's
browser — Claude doesn't need to render it.
