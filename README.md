# mssql-mcp-server

A Model Context Protocol server that lets Claude query Microsoft SQL Server
(on-prem, Azure SQL Database, or Azure SQL Managed Instance) safely, with
first-class **Microsoft Entra ID** authentication.

Read-only by default. Statement classifier blocks `EXEC`, `OPENROWSET`,
`xp_cmdshell`, multi-statement batches, etc. before pyodbc sees the SQL.
Row caps, query timeouts, and an append-only audit log come standard.

---

## Install (Windows)

```powershell
git clone <your-repo-url>
cd mssql-mcp-server
.\install.ps1
```

The installer asks a few questions, then takes care of:

- Python 3.12 via `winget` (if 3.11+ isn't already there)
- ODBC Driver 18 for SQL Server (via `winget`)
- `.venv` + `pip install -e .`
- `environments.json` with your first SQL Server target
- `claude mcp add -s user mssql ...` so **Claude Code** can see it
- Merging an `mssql` entry into `%APPDATA%\Claude\claude_desktop_config.json`
  so **Claude Desktop (Cowork mode)** can see it too
- `mssql-mcp-server doctor --skip-db --skip-token` to verify

Re-running is safe — every step skips work already done.

Then **open a new Claude Code conversation** (and/or restart Claude
Desktop) to see the `mssql` tools appear.

Useful flags:

| Flag | Effect |
|---|---|
| `-EnvironmentsFile <path>` | Use a pre-made registry instead of the wizard |
| `-NonInteractive` | Fail rather than prompt (CI) |
| `-SkipPython` / `-SkipOdbc` | Don't auto-install those |
| `-SkipClaudeRegister` | Skip **both** Claude Code and Claude Desktop registration |
| `-SkipCoworkRegister` | Skip only the Claude Desktop step (Code still gets registered) |

### Manual install (macOS / Linux)

```bash
# 1. Install Microsoft ODBC Driver 18 for SQL Server first:
#    https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server

git clone <your-repo-url>
cd mssql-mcp-server
python -m venv .venv
source .venv/bin/activate
pip install -e .

mssql-mcp-server init      # writes a single-server .env
mssql-mcp-server doctor    # verifies driver + connection
```

For **multi-environment** mode, copy `environments.example.json` to
`environments.json`, edit it, and set `MSSQL_ENVIRONMENTS_FILE=./environments.json`
in your `.env` (or export it). The server then picks up the registry
instead of `MSSQL_SERVER` / `MSSQL_DATABASE`.

Register with Claude Code:

```bash
claude mcp add \
  -e MSSQL_AUTH_MODE=entra_cli \
  -e MSSQL_READ_ONLY=true \
  -e MSSQL_ENVIRONMENTS_FILE=$PWD/environments.json \
  -e MSSQL_AUDIT_LOG_PATH=$PWD/logs/audit.jsonl \
  -e MSSQL_TOKEN_CACHE_PATH=$HOME/.cache/mssql-mcp/token-cache.bin \
  -s user mssql \
  $PWD/.venv/bin/mssql-mcp-server
```

`-s user` makes the server available across every project. All `-e` flags
must come before the server name and command path — that ordering is
required by `claude mcp add`.

---

## Using it from Cowork mode (Claude Desktop)

`install.ps1` already registers with **both** Claude Code and Claude
Desktop. The section below is only relevant if:

- You ran `install.ps1 -SkipCoworkRegister` and want to add Cowork later, or
- Claude Desktop's config got reset and you want to re-merge the entry, or
- You want to switch the Desktop entry's auth mode without redoing the full install.

### Refresh the Claude Desktop entry only — `register-cowork.ps1`

```powershell
.\register-cowork.ps1                  # safe merge into existing config
.\register-cowork.ps1 -DryRun          # preview without writing
.\register-cowork.ps1 -AuthMode entra_cli -Force
.\register-cowork.ps1 -ReadOnly false -AllowDdl true -Force
```

The script reads `%APPDATA%\Claude\claude_desktop_config.json`, merges in
an `mssql` entry under `mcpServers` (preserving every other server you
have), backs up the previous file, and prints a summary. Re-running is
a no-op unless you pass `-Force`. Restart Claude Desktop after.

Under the hood, both `install.ps1` and `register-cowork.ps1` route
through the same helper (`scripts\register-cowork.lib.ps1`) which calls
a small Python merge script (`scripts\merge_cowork_entry.py`). Doing the
JSON merge in Python sidesteps several PowerShell 5.1 quirks
(`ConvertFrom-Json -AsHashtable` missing, `PSCustomObject` type tests,
`OrderedDictionary.ContainsKey`).

> **Pre-req:** `register-cowork.ps1` uses the venv Python at
> `.venv\Scripts\python.exe`. Run `install.ps1` first so the venv exists.

### Manual path

If you'd rather edit the config by hand, follow the steps below — the
JSON to paste is also available stand-alone at
[`claude_desktop_config.snippet.json`](./claude_desktop_config.snippet.json).

### 1. Open `claude_desktop_config.json`

Path on Windows:

```
%APPDATA%\Claude\claude_desktop_config.json
```

Easiest route: in the Claude desktop app, **Settings → Developer →
"Edit Config"** opens the file directly. If it doesn't exist yet the app
will create a stub the first time you visit that screen.

### 2. Add the `mssql` server entry

Merge this into the file — don't replace it, preserve any other
`mcpServers` entries that are already there:

```json
{
  "mcpServers": {
    "mssql": {
      "command": "D:\\WorkSpace\\mcp_servers\\mcp_mssql\\.venv\\Scripts\\mssql-mcp-server.exe",
      "args": [],
      "env": {
        "MSSQL_AUTH_MODE": "entra_interactive",
        "MSSQL_READ_ONLY": "true",
        "MSSQL_MAX_ROWS": "1000",
        "MSSQL_QUERY_TIMEOUT": "30",
        "MSSQL_ENVIRONMENTS_FILE": "D:\\WorkSpace\\mcp_servers\\mcp_mssql\\environments.json",
        "MSSQL_AUDIT_LOG_PATH": "D:\\WorkSpace\\mcp_servers\\mcp_mssql\\logs\\audit.jsonl",
        "MSSQL_TOKEN_CACHE_PATH": "C:\\Users\\<you>\\.cache\\mssql-mcp\\token-cache.bin",
        "MCP_TRANSPORT": "stdio",
        "LOG_LEVEL": "INFO"
      }
    }
  }
}
```

- Backslashes must be doubled in JSON (`\\`).
- `command` points at the installer's `mssql-mcp-server.exe` entry point —
  the same one `install.ps1` registers with Claude Code. To invoke via the
  Python module instead, use `command: ".venv\\Scripts\\python.exe"` and
  `args: ["-m", "mssql_mcp"]`.
- `entra_interactive` is usually the right auth mode for Cowork on a
  desktop — browser popup with MFA once, then cached. Use `entra_cli` if
  you'd rather reuse your `az login` session, or `entra_service_principal`
  for unattended.
- Keep `MSSQL_READ_ONLY=true` unless you specifically need writes — the
  server doesn't register `execute_non_query` / `execute_ddl` at all when
  this is on.

### 3. Restart Claude Desktop

Fully quit and relaunch (tray icon → Quit, then reopen). MCP servers load
at app start, not per conversation — closing the window isn't enough.

### 4. Verify in a new Cowork conversation

In a fresh chat, ask:

> *"Are the mssql tools available? Call `server_info`."*

You should see `list_environments`, `list_tables`, `describe_table`,
`execute_query`, `explain_query`, etc. in the tool palette, and
`server_info` should return your SQL Server version + active auth mode.

After that, the data-plugin slash commands route through these tools
automatically:

> *"`/data:explore-data dbo.QB_QBANKS`"* — full table profile (nulls,
> distincts, top values, percentiles)
>
> *"`/data:write-query` top 20 quizzes by attempt count this week"* —
> generates T-SQL, runs it, returns results
>
> *"`/data:build-dashboard` of usage metrics from the last 90 days"* —
> queries via `execute_query`, renders an HTML dashboard artifact

### Troubleshooting Cowork integration

**Tools don't appear.** Check `%APPDATA%\Claude\logs\mcp*.log` — Claude
Desktop writes MCP startup errors there. Most common causes: bad path in
`command` (verify `.venv\Scripts\mssql-mcp-server.exe` exists) or JSON
syntax (trailing comma, single backslashes).

**MFA browser never appears.** Run `mssql-mcp-server doctor` from a
terminal once to pre-warm the token cache — subsequent Cowork sessions
reuse the cached token (~1× per hour).

**Confirm the server starts cleanly.** Run it stand-alone to surface
errors before relaunching Claude Desktop:

```powershell
cd D:\WorkSpace\mcp_servers\mcp_mssql
.\.venv\Scripts\mssql-mcp-server.exe
```

It should print the startup log lines (auth mode, environments registry,
mode/row-cap/timeout/audit path) and then wait on stdio. Ctrl-C to exit.

---

## Using it

In a Claude Code chat:

> *"List the mssql environments, then switch to dev and show me the databases."*
>
> *"Describe the `dbo.QB_QBANKS` table and run `SELECT * FROM it WHERE ACTIVE=1`."*
>
> *"Explain this query — show me the estimated plan."*

You can refer to environments, databases, schemas, tables, columns, and
indexes by name. Claude will pick the right tool automatically.

### Features

Every feature below is registered conditionally on `MSSQL_READ_ONLY` and
`MSSQL_ALLOW_DDL` at startup. The MCP server returns Pydantic-typed
responses (see [`src/mssql_mcp/models.py`](src/mssql_mcp/models.py)) so
Claude gets structured output, not free-form strings. Optional
parameters are shown with `?`.

#### Environment management (always on)

Synthesises a single `"default"` entry in single-server mode; lists the
full registry in `environments.json` mode.

##### `list_environments() → [EnvironmentInfo]`
Every configured target: `name`, `description`, `server`, `port`, `default_database`.
> *"What mssql environments are configured?"*

##### `current_environment() → CurrentEnvironment`
The env + database the server is currently connected to. Call this first
if multiple environments exist.
> *"Which mssql am I connected to right now?"*

##### `switch_environment(name, database?) → CurrentEnvironment`
Pivot to a different env. `database` overrides that env's default.
> *"Switch to prod."*
> *"Switch to qa and use AppDb_Snapshot."*

##### `switch_database(database) → CurrentEnvironment`
Switch DB inside the **current** env (no server change). Use
`switch_environment` first if you need a different server.
> *"Switch to tempdb."*

#### Schema introspection (always on)

##### `list_databases() → [string]`
User databases on the connected server. Excludes `master`, `tempdb`, `model`, `msdb`.
> *"Show me the databases."*

##### `list_schemas(database?) → [string]`
Schemas in the current DB (or a named DB).

##### `list_tables(schema?, name_pattern?) → [TableInfo]`
Tables + views. `name_pattern` is a SQL `LIKE` pattern. Row counts are
estimates from `sys.dm_db_partition_stats` and may lag actual `COUNT(*)`.
> *"List tables in dbo whose name starts with `QB_`."* → `list_tables(schema="dbo", name_pattern="QB_%")`

##### `describe_table(schema, table) → TableDescription`
Columns (type, nullability, identity, default), primary key, and a
ready-to-run sample `SELECT`.
> *"Describe `dbo.QB_QBANKS`."*

##### `list_indexes(schema, table) → [IndexInfo]`
Key columns, included columns, filter definition, and (when the DMV is
granted) fragmentation percentage.
> *"What indexes are on `dbo.Orders`?"*

##### `list_foreign_keys(schema, table) → [ForeignKeyInfo]`
FKs in **both directions** — outgoing FKs declared on the table plus
incoming FKs declared on other tables that reference it.
> *"Show all FK relationships touching `dbo.Customers`."*

#### Programmable-object inspection (always on)

##### `list_procedures(schema?, name_pattern?) → [ObjectInfo]`
User stored procedures (Microsoft-shipped excluded).

##### `list_functions(schema?, name_pattern?, kind?) → [ObjectInfo]`
User functions. `kind` narrows to `"scalar_function"` or `"table_function"`
— omit for both.

##### `list_views(schema?, name_pattern?) → [ObjectInfo]`
User views (Microsoft-shipped excluded).

##### `get_object_definition(schema, name) → ObjectDefinition?`
`CREATE` source for a procedure / function / view from
`sys.sql_modules`. Returns `null` if not found.
> *"Show the source of `dbo.usp_billing`."*

#### Cross-environment compare (registry mode only)

These read from **two** environments in a single call — no
`switch_environment` between them. The manager keeps one connection
pool per env alive on demand, so repeated `compare_*` calls hitting the
same envs reuse the existing connections.

##### `compare_procedure(env_a, env_b, schema, name, database_a?, database_b?) → ObjectDiff`
Unified diff of one stored procedure across two envs, plus
`a_line_count` / `b_line_count` / `a_missing` / `b_missing` flags.
> *"Compare `dbo.usp_billing` between dev and qa."*

##### `compare_function(env_a, env_b, schema, name, database_a?, database_b?) → ObjectDiff`
Same for user functions (scalar or TVF).

##### `compare_view(env_a, env_b, schema, name, database_a?, database_b?) → ObjectDiff`
Same for views.

##### `compare_table(env_a, env_b, schema, table, database_a?, database_b?) → TableDiff`
Structural compare: columns (type / nullability / identity / default),
indexes (key + included columns, filter), foreign keys. Only differences
are listed — each entry tagged `added` / `removed` / `changed`.
> *"Has `dbo.Users` schema drifted between qa and prod?"*

#### Query execution

##### `execute_query(sql, params?, format?) → QueryResult | CompactQueryResult`
Read-only T-SQL — `SELECT` or CTE-led `SELECT`. The statement classifier
blocks multi-statement batches, `EXEC`, `OPENROWSET`, `BACKUP`,
`xp_cmdshell`, etc. before pyodbc sees the SQL.

Always prefer parameterised queries — use `?` placeholders, pass each
value as a `QueryParam`. Inlining values is allowed but discouraged.

`format`:
- `"dict"` *(default)* — rows as `[{col: value, ...}, ...]`, human-friendly.
- `"compact"` — rows as `[[value, value, ...], ...]` aligned to `columns`.
  Saves ~40–60% of tokens on wide or many-row results — semantics
  identical, just no repeated column names per row.

Capped at `MSSQL_MAX_ROWS` (default 1000); `truncated: true` when the
cap is hit.

```json
execute_query(
  sql="SELECT TOP 50 * FROM dbo.Orders WHERE Status = ? AND Total > ?",
  params=[{"value": "Pending"}, {"value": 500.00}],
  format="compact"
)
```

> *"Run `SELECT TOP 50 * FROM dbo.Orders WHERE Status='Pending'` and return it compact."*

##### `explain_query(sql) → {plan_xml, summary}`
Estimated execution plan via `SET SHOWPLAN_XML ON`. No execution.
> *"Explain `SELECT * FROM dbo.bigtable WHERE id = 1`."*

##### `server_info() → ServerInfo`
SQL Server version, current database / user, `ORIGINAL_LOGIN()`,
**active MCP mode** (`read_only` / `write` / `ddl`), **active auth mode**.
> *"What server are we on and what mode is the MCP in?"*

#### Write operations (only when `MSSQL_READ_ONLY=false`)

##### `execute_non_query(sql, params?) → NonQueryResult`
`INSERT` / `UPDATE` / `DELETE` / `MERGE`. Wrapped in an explicit
transaction. If the statement would affect more than
`MSSQL_MAX_AFFECTED_ROWS` (default 10 000), the change is **rolled back**
before commit.

```json
execute_non_query(
  sql="UPDATE dbo.Users SET Status = ? WHERE Id = ?",
  params=[{"value": "Active"}, {"value": 5611275}]
)
```

#### DDL (only when `MSSQL_READ_ONLY=false` **and** `MSSQL_ALLOW_DDL=true`)

##### `execute_ddl(sql) → DdlResult`
Single `CREATE` / `ALTER` / `DROP` / `TRUNCATE` statement. Audited at
`WARNING` level so DDL events stand out in the audit log.

#### CLI subcommands (host process, not MCP tools)

| Command | Purpose |
|---------|---------|
| `mssql-mcp-server serve` | Default. Run the MCP server on the configured transport (`stdio` or `http`). |
| `mssql-mcp-server init [--env-path PATH]` | Interactive wizard: detects ODBC drivers, asks 3–4 questions, writes a starter `.env`, prints the Claude Desktop config block ready to paste. |
| `mssql-mcp-server doctor [--skip-token] [--skip-db]` | Preflight: Python → ODBC → config → audit log → Entra token → real connection → `SELECT 1`. PASS / WARN / FAIL / INFO with one-line `fix:` hints. Exit 0 on all PASS, 1 on any FAIL — scriptable in CI. |
| `mssql-mcp-server --version` | Print the installed version. |

### Release-file drift compare (skill + script)

For comparing a **consolidated release SQL file** (the desired state)
against a live environment (the current state) — the common use case
when reviewing a release — this repo ships a Claude Code skill plus a
standalone CLI:

| Asset | Purpose |
|---|---|
| [`.claude/skills/sql-compare/SKILL.md`](.claude/skills/sql-compare/SKILL.md) | Skill that fires on *"compare X.sql with stg"* / *"drift"* / *"/sql-compare"* and drives the whole workflow end-to-end. Strictly read-only. Token-efficient: all bodies and diffs go to disk; only a small summary enters the conversation. |
| [`scripts/compare_release/compare.py`](scripts/compare_release/compare.py) | The CLI the skill invokes. Parses the file, ingests a `sys.sql_modules` dump produced by `execute_query`, normalises both sides (strips comments / brackets / whitespace, one keyword per line), and produces a clickable HTML drift report. |

Trigger phrases the skill recognises: *"compare"*, *"compare X.sql with stg"*,
*"drift"*, *"release diff"*, *"what would deploy"*, *"check if file matches DB"*.

What lands on disk:

```
build/drift/<timestamp>/
├── index.html         ← summary table, click any DRIFT row
├── <OBJECT>.html      ← per-object change blocks: added / only-in-staging / changed,
│                        with surrounding identical-on-both-sides context
├── <OBJECT>.txt       ← plain-text equivalent (grep-friendly)
└── report.json        ← machine-readable summary + per-object change counts
```

Statuses surfaced: `MATCH` / `DRIFT` / **`staging ahead`** (heuristic
from audit-history dates: staging has dates the file doesn't — the file
would regress those changes) / `missing` (object in file, not on the
target).

See [`scripts/compare_release/README.md`](scripts/compare_release/README.md)
for the manual `python compare.py …` invocation and the recommended
staging-dump query.

---

## Configuration

### Multiple servers via `environments.json`

```json
{
  "default": "dev",
  "environments": [
    {
      "name": "dev",
      "server": "your-dev-mssql.<dns-zone>.database.windows.net",
      "default_database": "master"
    },
    {
      "name": "qa",
      "server": "your-qa-mssql.<dns-zone>.database.windows.net",
      "default_database": "AppDb"
    }
  ]
}
```

Set `MSSQL_ENVIRONMENTS_FILE=./environments.json` and Claude can pivot
between these targets at runtime with `switch_environment`. Only servers
in this file are reachable — the registry is an allow-list.

See [`environments.example.json`](environments.example.json).

### Authentication

Pick the auth mode that fits where the server runs:

| Auth mode | Use when |
|---|---|
| `entra_cli` | Local dev, you already `az login` |
| `entra_interactive` | Local dev, no Azure CLI (browser popup, cached) |
| `entra_device_code` | Headless / SSH session with MFA |
| `entra_service_principal` | Production, secret-based |
| `entra_service_principal_cert` | Production, cert-based |
| `entra_managed_identity` | Server runs inside Azure |
| `entra_default` | Auto-detect (`DefaultAzureCredential` chain) |
| `sql` | Legacy SQL login (dev only) |
| `windows` | Domain-joined Windows host |

**MFA happens at token acquisition (~1× per hour), not per query.** The
Entra principal must exist as a contained user in the target database
with at least `db_datareader`:

```sql
CREATE USER [<your-app-or-email>] FROM EXTERNAL PROVIDER;
ALTER ROLE db_datareader ADD MEMBER [<your-app-or-email>];
```

### Key environment variables

See `.env.example` and [the wizard output](src/mssql_mcp/cli.py) for the
full list. The minimum you need:

| Variable | Default | Notes |
|---|---|---|
| `MSSQL_SERVER` | — | Required unless `MSSQL_ENVIRONMENTS_FILE` is set |
| `MSSQL_DATABASE` | — | Required unless `MSSQL_ENVIRONMENTS_FILE` is set |
| `MSSQL_AUTH_MODE` | `entra_default` | One of the modes above |
| `MSSQL_READ_ONLY` | `true` | Master switch for write tools |
| `MSSQL_ALLOW_DDL` | `false` | DDL stays off unless this is `true` |
| `MSSQL_MAX_ROWS` | `1000` | Row cap per query |
| `MSSQL_QUERY_TIMEOUT` | `30` | Seconds |
| `MSSQL_AUDIT_LOG_PATH` | `./logs/audit.jsonl` | Append-only, daily rotation |

---

## Troubleshooting

**`Login failed for user '<token-identified principal>'`**
Your Entra principal isn't mapped as a contained user in the target DB.
Run `CREATE USER [...] FROM EXTERNAL PROVIDER;` + grant a role.

**`TCP Provider: The wait operation timed out`**
Network reachability — usually VPN isn't connected, or you're hitting
the private endpoint port (1433) without a route. For Azure SQL MI
public endpoint, use port 3342.

**Tools don't show up in Claude after install**
Open a *new* conversation (MCP tools load at session start). Run
`claude mcp list` — if it shows `✗ Failed to connect`, run
`mssql-mcp-server doctor` to diagnose.

**`SSL Provider: certificate chain ... untrusted`**
Driver 18 forces `Encrypt=yes`. For dev with self-signed certs only,
set `MSSQL_TRUST_SERVER_CERT=yes`. Never in production.

---

## Security posture

- **Read-only by default**, write tools not registered when
  `MSSQL_READ_ONLY=true`.
- **No string-built SQL** — every parameter goes through `?` placeholders.
- **Statement classifier** runs before every execute; blocks multi-statement
  batches, `EXEC`, `OPENROWSET`, `BACKUP`, `xp_cmdshell`, etc.
- **Row + time caps** — `MSSQL_MAX_ROWS`, `MSSQL_QUERY_TIMEOUT`.
- **Token-aware pool** — evicts connections before tokens expire; one
  silent retry on mid-query expiry.
- **Audit log** — JSONL, `0600` on POSIX, never contains tokens.
- **Sanitised errors** — server names / paths / stack traces stripped
  before returning to the LLM.
- **Environment allow-list** — model can't be coerced into pointing the
  connection at servers you didn't configure.

---

## Project layout

```
mssql-mcp-server/
├── install.ps1                          # one-shot Windows installer (sets up everything)
├── register-cowork.ps1                  # thin refresher for the Claude Desktop entry
├── scripts/
│   ├── register-cowork.lib.ps1          # shared PS helper, dot-sourced by both .ps1 scripts
│   └── merge_cowork_entry.py            # Python helper that actually merges the JSON
├── claude_desktop_config.snippet.json   # manual paste-in alternative to register-cowork.ps1
├── environments.example.json            # registry template (3 envs)
├── environments.json                    # your real registry (gitignored)
├── .env.example                         # single-server config template
├── Dockerfile                           # for HTTP-transport deployments
├── pyproject.toml
├── src/mssql_mcp/
│   ├── server.py                  # FastMCP wiring
│   ├── manager.py                 # runtime env switching + multi-env pool cache
│   ├── environments.py            # registry model + loader
│   ├── config.py                  # Settings (pydantic-settings)
│   ├── auth.py                    # TokenProvider (Entra)
│   ├── db.py                      # classifier + token-aware pool
│   ├── audit.py                   # JSONL logger
│   ├── cli.py                     # init / doctor / serve subcommands
│   ├── models.py                  # Pydantic input/output (incl. compact + diff)
│   └── tools/
│       ├── query.py               # execute_query / execute_non_query / execute_ddl
│       ├── schema.py              # list_databases / list_schemas / list_tables / describe_table
│       ├── indexes.py             # list_indexes / list_foreign_keys
│       ├── objects.py             # list_procedures / list_functions / list_views / get_object_definition
│       ├── compare.py             # compare_procedure / compare_function / compare_view / compare_table
│       ├── diagnostics.py         # explain_query / server_info
│       └── environments.py        # list_environments / current_environment / switch_*
└── tests/                         # 169 tests; 152 unit + 17 integration
```

### Single-server vs registry mode

| Mode | Config file | When |
|---|---|---|
| **Single-server** | `.env` (written by `mssql-mcp-server init`) | One SQL Server, no switching needed |
| **Registry** | `environments.json` (written by `install.ps1` and edited later) | Multiple servers, runtime switching via `switch_environment` |

When `MSSQL_ENVIRONMENTS_FILE` is set, `MSSQL_SERVER` / `MSSQL_DATABASE`
become optional and the active environment provides them.

---

## Development

```bash
pip install -e ".[dev]"
ruff check src tests
mypy src
pytest                                # unit tests
MSSQL_INTEGRATION_TESTS=1 pytest      # add integration tests
```

Integration tests provision a temporary table in `dbo` and tear it down
— point them at a throwaway database, not production.
