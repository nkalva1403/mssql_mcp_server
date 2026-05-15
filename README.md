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

### Tools available

| Tool | What it does |
|---|---|
| `list_environments`, `current_environment`, `switch_environment`, `switch_database` | Navigate between configured SQL Server targets |
| `list_databases`, `list_schemas`, `list_tables`, `describe_table` | Schema introspection |
| `list_indexes`, `list_foreign_keys` | Per-table metadata |
| `list_procedures`, `list_functions`, `list_views`, `get_object_definition` | Programmable-object inspection (source from `sys.sql_modules`) |
| `compare_procedure`, `compare_function`, `compare_view`, `compare_table` | Cross-environment diff — registry mode only |
| `execute_query` | Read-only SELECT / CTE, row-capped. Accepts `format="compact"` for columnar rows (~40-60% smaller payload) |
| `execute_non_query` | INSERT / UPDATE / DELETE / MERGE — registered only when `MSSQL_READ_ONLY=false` |
| `execute_ddl` | CREATE / ALTER / DROP / TRUNCATE — needs `READ_ONLY=false` **and** `ALLOW_DDL=true` |
| `explain_query` | Estimated plan (no execution) |
| `server_info` | Version, DB, user, `ORIGINAL_LOGIN()`, active MCP mode, active auth mode |

### Cross-environment comparison

In registry mode, the `compare_*` tools read from two configured
environments in a single call without a `switch_environment`. The
manager keeps one connection pool per environment alive on demand, so
asking `compare_procedure("dev", "prod", "dbo", "usp_billing")` opens
both servers concurrently and returns a unified diff. Examples:

> *"Compare `dbo.usp_billing` between dev and qa."* — runs
> `compare_procedure(env_a="dev", env_b="qa", schema="dbo", name="usp_billing")`,
> returns a unified diff plus `a_line_count` / `b_line_count` /
> `a_missing` / `b_missing` flags.
>
> *"Has the `Users` table schema drifted between qa and prod?"* — runs
> `compare_table`, returns column / index / foreign-key diffs (each
> entry tagged `added` / `removed` / `changed`, identical fields
> omitted).

### Compact response format

`execute_query` accepts an optional `format` argument:

- `"dict"` (default): rows are `[{col: value, ...}, ...]` — human-friendly.
- `"compact"`: rows are `[[value, value, ...], ...]` aligned to
  `columns`. On a 100-row × 10-column result this typically halves the
  JSON payload, which matters for token-bound AI usage. Identical
  semantics — just no repeated column names per row.

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

## Diagnostics

```bash
mssql-mcp-server doctor
```

Runs through: Python version → ODBC driver → config → audit log → Entra
token → real connection → `SELECT 1`. Each check is `PASS` / `WARN` /
`FAIL` / `INFO`, with a one-line `fix:` hint on failures.

Flags:

- `--skip-token` — skip the token acquisition step
- `--skip-db` — driver + config only, no connection

Exit 0 on full PASS, exit 1 on any FAIL. Scriptable in CI.

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
