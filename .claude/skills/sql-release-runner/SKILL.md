---
name: sql-release-runner
description: Run a release's DB script files, in the given order, statement batch by statement batch (split on GO) against Dev2 and then QA2 through the MSSQL MCP `run_release_batch` tool, and deliver a plain-English HTML report of every step - done / failed (with the real SQL Server error and file line) / refused / skipped. Pre-flight flags destructive statements, missing re-run checks and data changes that need a backup; skips any batch that already ran successfully on that database. Asks for the target database every time. Use when the user says "run these scripts on dev2", "execute the release scripts", "deploy DB scripts to QA2", "run the DB scripts in order and give me a report", "/sql-release-runner", or shares ordered .sql files to be run. Never targets prod or staging.
---

# SQL release runner

Runs reviewed release scripts on **Dev2** (`dev2`, `uw_dev_in2_cms_content_db`) and **QA2**
(`qa-in2`, `uw_qa_in2_cms_content_db`), then reports. It cannot reach any other database: the MCP
server refuses prod/staging names at startup and only registers these two as release targets.

## Fixed settings

| What | Value |
|---|---|
| Python (stdlib-only scripts) | `D:/WorkSpace/mcp_servers/mcp_mssql/.venv/Scripts/python.exe` |
| Runner | `D:/WorkSpace/mcp_servers/mcp_mssql/scripts/release_runner/runner.py` |
| Run root (= server `MSSQL_RELEASE_BATCH_ROOT`) | `D:/WorkSpace/CMS_Project/release-runs` |
| Journal (= server `MSSQL_RELEASE_JOURNAL_DIR`) | `D:/WorkSpace/CMS_Project/release-runs/_journal` |
| Dev2 | tool `mcp__mssql__run_release_batch`, `environment="dev2"`, db `uw_dev_in2_cms_content_db` |
| QA2 | tool `mcp__mssql-qa__run_release_batch`, `environment="qa-in2"`, db `uw_qa_in2_cms_content_db` |

Below, `RUNNER` means `"<Python>" "<Runner>"`.

## Hard rules

1. **Ask which database every time** (AskUserQuestion: Dev2 / QA2), even if the user named one
   earlier in the conversation. Never infer it. Never offer prod, staging, Dev1, QA1 or QA-US.
2. **Only `run_release_batch` changes data.** Never use `execute_query`, `execute_non_query` or
   `execute_ddl` to run a script, a fix, or a "quick check" that writes.
3. **Never retype SQL.** Always pass `batch_file` from the plan. Never pass `sql=` for release work,
   never edit, reorder, merge or "fix" a script. If a script is wrong, report it - the authors fix it.
4. **No backups.** Flag data-changing steps; do not create backup tables or run BACKUP.
5. **Continue on failure.** A failed step is recorded and the run moves on. Never retry a failed step
   in the same run, and never re-run a step to "see if it works now".
6. **QA2 only after Dev2 is reported** and the user explicitly says to go ahead with QA2.
7. **Results come only from the report.** The server journals every call; `runner.py report`
   builds the report from that journal. Never write statuses, counts or errors from memory.
8. Do not create JIRA/Confluence items or send the report anywhere unless asked.

## Workflow

### 0. Check the tool is loaded
The tool is `run_release_batch` on the `mssql` / `mssql-qa` servers. If it is not available (load it
with ToolSearch `select:mcp__mssql__run_release_batch,mcp__mssql-qa__run_release_batch`), stop: the
MCP servers need a restart (start a new Claude Code session) after a config change.

### 1. Collect the files and their order
Accept a folder + order list, an explicit list, or an order file (one path per line, `#` comments).
If the order is not stated explicitly, ask - never guess it from file names. Echo the numbered list
back. Write it to `<run root>/_orders/<release>.txt` and use `--order-file`.

### 2. Ask the target (always)
AskUserQuestion with exactly two options: **Dev2** (`dev2`, `uw_dev_in2_cms_content_db`) and
**QA2** (`qa-in2`, `uw_qa_in2_cms_content_db`).

### 3. Plan (offline, nothing touches the database)
```bash
RUNNER plan --order-file "<order.txt>" --env dev2 --database uw_dev_in2_cms_content_db \
  --name "<release name>" --root "D:/WorkSpace/CMS_Project/release-runs" \
  --journal-dir "D:/WorkSpace/CMS_Project/release-runs/_journal"
```
It prints `RUN_ID` / `RUN_DIR` and writes `preflight.md`, `plan.json` and `batches/*.sql`.
What the plan does:
- splits each file on `GO` (ignores `GO` inside comments and strings; `GO n` = repeat n times);
- turns SET-only batches (`SET QUOTED_IDENTIFIER ON` ...) into session settings for the rest of that file;
- **skips** a batch whose exact text already ran successfully on this database (journal ledger), and
  an exact duplicate of an earlier batch in the same release;
- flags: refused-by-server, SQLCMD syntax, temp table from another batch, UPDATE/DELETE without
  WHERE, DROP/TRUNCATE/DROP COLUMN, data changes needing a backup, no re-run check, other-database
  references, `GO n`.

### 4. Pre-flight review - the gate
Read `preflight.md` and show the user, briefly: counts (to run / skipped), then the flagged steps
grouped dangerous-first, each as `label - file lines - what it does - why flagged`. Then ask:
- which flagged steps to exclude (if any). Record them with
  `RUNNER exclude --run "<RUN_DIR>" --labels F01-B004,F02-B007 --reason "<user's reason>"`;
- an explicit go: "Run N steps on <Dev2/QA2> now?" Nothing runs without a clear yes.

### 5. Execute
Loop until nothing remains:
```bash
RUNNER remaining --run "<RUN_DIR>" --limit 25
```
Each JSON line is one step. For each, in order, call the target's tool:
`run_release_batch(environment=<env>, batch_file=<batch_file>, label=<label>,
session_settings=<session_settings>, run_id=<RUN_ID>)` - once, or `repeat` times if repeat > 1.
- Do not narrate each step. Every 25 steps give one line: `75/180 done - 3 failed so far`.
- `failed` -> keep going. `blocked` for a step -> keep going (it is reported).
- **Stop and tell the user** if a call is blocked for a configuration reason (`not a release target`,
  `batch_file needs`, `must be under the release batch root`) or if 3 calls in a row fail with
  login/connection/timeout errors.
- Heavy data steps: pass `timeout_seconds` (default 600, max 3600) only if a step timed out and the
  user agrees to re-run it in a new run.

### 6. Report
```bash
RUNNER report --run "<RUN_DIR>" --out "<RUN_DIR>/report.html" --title "Database Script Run Report - <release> - <env>"
```
Give the user the report path and a short chat summary: the verdict line and, for each failed or
refused step, `label - file:line - plain reason`. For Dev2 then QA2, after QA2 also build a combined
report with both runs side by side (Dev2 first):
`RUNNER report --run "<dev2 RUN_DIR>" --run "<qa2 RUN_DIR>" --out "<qa2 RUN_DIR>/report-dev2-qa2.html"`.
Then ask whether to continue to QA2 (step 2 again - ask the target, new plan, new gate).

### Resume / re-run
- Interrupted run: `remaining` on the same `RUN_DIR` lists what is left; continue step 5.
- After the authors fix scripts: start a **new plan**. Batches that already succeeded are skipped
  automatically (same text), so only the changed or failed ones run.

## How a step runs (tell the user when relevant)
- One `GO` batch per call, on its own fresh connection, in the locked database. SET-only batches are
  carried forward; temp tables do not survive between batches.
- The first error stops that batch and is returned with its number, severity, line and message;
  statements before the error that were not in a transaction stay applied; an open transaction is
  rolled back.
- The server refuses (status `blocked`, nothing runs): `USE`, references to another environment's
  database, four-part/linked-server names, BACKUP/RESTORE, database/login-level DDL, xp_cmdshell,
  OPENQUERY/OPENROWSET, DBCC other than CHECKIDENT.
- About 3 seconds of connection overhead per step: 300 steps is roughly 15-20 minutes.
- The guard reads the batch text, so it cannot see inside dynamic SQL (`EXEC('...')`) - mention
  that a step uses dynamic SQL when the pre-flight shows one.
