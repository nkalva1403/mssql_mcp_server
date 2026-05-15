<#
.SYNOPSIS
    One-shot installer for mssql-mcp-server on Windows.

.DESCRIPTION
    Installs prerequisites if missing (Python 3.11+, Microsoft ODBC Driver 18
    for SQL Server via winget), creates a Python virtual environment, installs
    the package in editable mode, prompts for one or more SQL Server
    environments to write into environments.json, then registers the server
    with Claude Code via `claude mcp add` at user scope so it works across all
    your projects.

    Re-running is safe: each step skips work that's already done.

.PARAMETER SkipPython
    Don't attempt to install Python via winget. Fail if 3.11+ is not present.

.PARAMETER SkipOdbc
    Don't attempt to install the ODBC driver. Fail if it's not present.

.PARAMETER SkipClaudeRegister
    Don't run `claude mcp add` — just install the package + environments file.
    Skips Cowork registration too.

.PARAMETER SkipCoworkRegister
    Don't merge into Claude Desktop's config. Claude Code registration
    still runs (unless -SkipClaudeRegister is also set).

.PARAMETER EnvironmentsFile
    Path to an existing environments.json to copy into the project root.
    When given, the wizard is skipped.

.PARAMETER NonInteractive
    Fail (rather than prompt) when input is needed. Useful in CI.

.EXAMPLE
    .\install.ps1
    Interactive install: prompts for one environment, registers with Claude Code.

.EXAMPLE
    .\install.ps1 -EnvironmentsFile .\environments.example.json -NonInteractive
    Idempotent install for an already-configured repo.
#>

[CmdletBinding()]
param(
    [switch]$SkipPython,
    [switch]$SkipOdbc,
    [switch]$SkipClaudeRegister,
    [switch]$SkipCoworkRegister,
    [string]$EnvironmentsFile,
    [switch]$NonInteractive
)

$ErrorActionPreference = 'Stop'

function Info($msg) { Write-Host "[ INFO ]  $msg" -ForegroundColor Cyan }
function Step($msg) { Write-Host ""; Write-Host "=== $msg ===" -ForegroundColor White }
function Ok($msg)   { Write-Host "[  OK  ]  $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "[ WARN ]  $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "[ FAIL ]  $msg" -ForegroundColor Red; exit 1 }

function Test-Cmd($cmd) {
    return ($null -ne (Get-Command $cmd -ErrorAction SilentlyContinue))
}

# Refresh PATH from the registry so newly-installed tools are visible without
# a shell restart. winget installs update HKCU/HKLM but not the current process.
function Refresh-EnvPath {
    $machine = [Environment]::GetEnvironmentVariable('PATH', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('PATH', 'User')
    $env:Path = ($machine, $user -join ';')
}

$repoRoot = $PSScriptRoot
Set-Location $repoRoot

Write-Host ""
Write-Host "mssql-mcp-server installer" -ForegroundColor White
Write-Host "==========================" -ForegroundColor White
Write-Host "Repo: $repoRoot"

# --- 1. winget --------------------------------------------------------------

Step 'Checking winget'
if (-not (Test-Cmd winget)) {
    Fail "winget not found. Install 'App Installer' from the Microsoft Store, then re-run."
}
Ok 'winget available'

# --- 2. Python 3.11+ --------------------------------------------------------

Step 'Checking Python 3.11+'
$pythonExe = $null
foreach ($candidate in @('py', 'python', 'python3')) {
    if (Test-Cmd $candidate) {
        try {
            $ver = (& $candidate -3 --version 2>&1)
            if ($LASTEXITCODE -ne 0 -or -not ($ver -match 'Python ')) {
                $ver = (& $candidate --version 2>&1)
            }
            if ($ver -match 'Python (\d+)\.(\d+)') {
                $major = [int]$Matches[1]; $minor = [int]$Matches[2]
                if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 11)) {
                    $pythonExe = $candidate
                    Ok ("{0} -> Python {1}.{2}" -f $candidate, $major, $minor)
                    break
                }
            }
        } catch {}
    }
}

if (-not $pythonExe) {
    if ($SkipPython) {
        Fail 'Python 3.11+ missing and -SkipPython was set.'
    }
    Info 'Installing Python 3.12 via winget (this may prompt for elevation)...'
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        Fail "winget Python install failed (exit $LASTEXITCODE). Install Python 3.11+ manually."
    }
    Refresh-EnvPath
    if (Test-Cmd py) {
        $pythonExe = 'py'
    } elseif (Test-Cmd python) {
        $pythonExe = 'python'
    } else {
        Fail 'Python installed but not on PATH. Open a new terminal and re-run.'
    }
    Ok "Python installed via winget"
}

# --- 3. ODBC Driver 18 ------------------------------------------------------

Step 'Checking Microsoft ODBC Driver 18 for SQL Server'
$hasOdbc18 = $false
# Probe the ODBC driver registry — works even before pyodbc is installed.
$odbcRegPaths = @(
    'HKLM:\SOFTWARE\ODBC\ODBCINST.INI\ODBC Driver 18 for SQL Server',
    'HKLM:\SOFTWARE\WOW6432Node\ODBC\ODBCINST.INI\ODBC Driver 18 for SQL Server'
)
foreach ($p in $odbcRegPaths) {
    if (Test-Path $p) { $hasOdbc18 = $true; break }
}

if ($hasOdbc18) {
    Ok 'ODBC Driver 18 is installed'
} else {
    if ($SkipOdbc) {
        Fail 'ODBC Driver 18 missing and -SkipOdbc was set.'
    }
    Info 'Installing Microsoft ODBC Driver 18 via winget...'
    winget install -e --id Microsoft.ODBCDriver.18.SqlServer --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        Warn "First winget id failed; trying alternate id..."
        winget install -e --id Microsoft.OdbcDriver18ForSqlServer --accept-package-agreements --accept-source-agreements
    }
    if ($LASTEXITCODE -ne 0) {
        Fail "ODBC Driver 18 install failed. Install manually from https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server"
    }
    Ok 'ODBC Driver 18 installed'
}

# --- 4. venv ----------------------------------------------------------------

Step 'Setting up virtual environment'
$venvDir = Join-Path $repoRoot '.venv'
$venvPython = Join-Path $venvDir 'Scripts\python.exe'
$venvServer = Join-Path $venvDir 'Scripts\mssql-mcp-server.exe'

if (-not (Test-Path $venvPython)) {
    Info "Creating venv at $venvDir"
    & $pythonExe -3 -m venv $venvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPython)) {
        Fail 'venv creation failed.'
    }
}
Ok "venv ready at $venvDir"

# --- 5. pip install ---------------------------------------------------------

Step 'Installing mssql-mcp-server into the venv'
& $venvPython -m pip install --upgrade pip --quiet
if ($LASTEXITCODE -ne 0) { Fail 'pip upgrade failed.' }
& $venvPython -m pip install -e . --quiet
if ($LASTEXITCODE -ne 0) { Fail 'pip install -e . failed.' }
Ok 'package installed in editable mode'

# --- 6. environments.json --------------------------------------------------

Step 'Configuring environments.json'
$envJsonPath = Join-Path $repoRoot 'environments.json'

if ($EnvironmentsFile) {
    if (-not (Test-Path $EnvironmentsFile)) { Fail "EnvironmentsFile not found: $EnvironmentsFile" }
    $srcAbs = (Resolve-Path $EnvironmentsFile).Path
    $dstAbs = if (Test-Path $envJsonPath) { (Resolve-Path $envJsonPath).Path } else { $envJsonPath }
    if ($srcAbs -ieq $dstAbs) {
        Ok "Using existing $envJsonPath (source = destination)"
    }
    else {
        Copy-Item $EnvironmentsFile $envJsonPath -Force
        Ok "Copied $EnvironmentsFile -> $envJsonPath"
    }
}
elseif (Test-Path $envJsonPath) {
    Ok "Using existing $envJsonPath"
}
else {
    if ($NonInteractive) {
        Fail "No environments.json found and -NonInteractive was specified."
    }
    Write-Host ""
    Info "Define your first SQL Server environment (you can add more later by editing environments.json)."
    $envName = (Read-Host '  Name (short id, e.g. dev)').Trim()
    if (-not $envName) { $envName = 'dev' }
    $envServer = (Read-Host '  Server hostname (e.g. myserver.database.windows.net)').Trim()
    if (-not $envServer) { Fail 'Server is required.' }
    $envPort = (Read-Host '  Port [1433]').Trim()
    if (-not $envPort) { $envPort = '1433' }
    $envDatabase = (Read-Host '  Default database [master]').Trim()
    if (-not $envDatabase) { $envDatabase = 'master' }
    $envDesc = (Read-Host '  Description (optional)').Trim()

    $envObject = [ordered]@{
        name = $envName
        server = $envServer
        port = [int]$envPort
        default_database = $envDatabase
    }
    if ($envDesc) { $envObject['description'] = $envDesc }

    $payload = [ordered]@{
        default = $envName
        environments = @($envObject)
    }
    # PowerShell 5 ConvertTo-Json adds a BOM; write without one.
    $json = $payload | ConvertTo-Json -Depth 5
    [System.IO.File]::WriteAllText($envJsonPath, $json, [System.Text.UTF8Encoding]::new($false))
    Ok "Wrote $envJsonPath"
}

# --- 7. Register with Claude Code ------------------------------------------

Step 'Registering with Claude Code'

if ($SkipClaudeRegister) {
    Warn 'Skipping `claude mcp add` (-SkipClaudeRegister was set).'
}
elseif (-not (Test-Cmd claude)) {
    Warn 'claude CLI not found. Install Claude Code, then run:'
    Write-Host ""
    Write-Host "  claude mcp add ``"
    Write-Host "    -e MSSQL_AUTH_MODE=entra_interactive ``"
    Write-Host "    -e MSSQL_READ_ONLY=true ``"
    Write-Host "    -e MSSQL_ENVIRONMENTS_FILE=$envJsonPath ``"
    Write-Host "    -e ""MSSQL_AUDIT_LOG_PATH=$repoRoot\logs\audit.jsonl"" ``"
    Write-Host "    -e ""MSSQL_TOKEN_CACHE_PATH=$env:USERPROFILE\.cache\mssql-mcp\token-cache.bin"" ``"
    Write-Host "    -s user mssql ``"
    Write-Host "    $venvServer"
}
else {
    & claude mcp remove mssql -s user 2>$null | Out-Null
    $tokenCachePath = Join-Path $env:USERPROFILE '.cache\mssql-mcp\token-cache.bin'
    $auditLogPath   = Join-Path $repoRoot 'logs\audit.jsonl'

    & claude mcp add `
        -e "MSSQL_AUTH_MODE=entra_interactive" `
        -e "MSSQL_READ_ONLY=true" `
        -e "MSSQL_MAX_ROWS=1000" `
        -e "MSSQL_QUERY_TIMEOUT=30" `
        -e "MSSQL_ENVIRONMENTS_FILE=$envJsonPath" `
        -e "MSSQL_AUDIT_LOG_PATH=$auditLogPath" `
        -e "MSSQL_TOKEN_CACHE_PATH=$tokenCachePath" `
        -e "MCP_TRANSPORT=stdio" `
        -e "LOG_LEVEL=INFO" `
        -s user `
        mssql `
        $venvServer
    if ($LASTEXITCODE -ne 0) { Fail "claude mcp add failed (exit $LASTEXITCODE)." }
    Ok 'Registered mssql with Claude Code at user scope'
}

# --- 7b. Register with Claude Desktop (Cowork mode) ------------------------

Step 'Registering with Claude Desktop (Cowork)'

if ($SkipClaudeRegister -or $SkipCoworkRegister) {
    Warn 'Skipping Claude Desktop registration.'
}
else {
    $coworkLib = Join-Path $repoRoot 'scripts\register-cowork.lib.ps1'
    if (-not (Test-Path $coworkLib)) {
        Warn ('Cowork helper not found at ' + $coworkLib + ' - skipping Desktop registration.')
    }
    else {
        . $coworkLib
        $coworkConfig = Join-Path $env:APPDATA 'Claude\claude_desktop_config.json'
        Invoke-CoworkRegistration `
            -RepoRoot $repoRoot `
            -ConfigPath $coworkConfig `
            -AuthMode 'entra_interactive' `
            -ReadOnly 'true' `
            -AllowDdl 'false' `
            -MaxRows '1000' `
            -QueryTimeout '30' `
            -Force
    }
}

# --- 8. Doctor (skipping DB to avoid VPN/auth prompts during install) ------

Step 'Verifying install'
& $venvPython -m mssql_mcp doctor --skip-db --skip-token

Write-Host ""
Ok 'Install complete.'
Write-Host ""
Write-Host 'Next steps:' -ForegroundColor White
Write-Host '  1. Open a NEW Claude Code conversation - the mssql tools will be loaded'
Write-Host '  2. Try asking Claude: List the environments on the mssql server'
Write-Host '  3. Then: Switch to environment <name> and show me the databases'
Write-Host ''
Write-Host 'To add or remove environments later, edit:' -ForegroundColor White
Write-Host ('  ' + $envJsonPath)
