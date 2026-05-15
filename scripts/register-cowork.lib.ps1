<#
Shared Cowork registration helper, dot-sourced by both install.ps1 and
register-cowork.ps1. Builds the mssql entry, writes it to a temp file
(to avoid PowerShell-to-native-exe quote stripping), and invokes the
Python merge helper at scripts\merge_cowork_entry.py.

Exposes:  Invoke-CoworkRegistration -RepoRoot ... -AuthMode ... etc.
#>

function Invoke-CoworkRegistration {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string]$RepoRoot,
        [Parameter(Mandatory)] [string]$ConfigPath,
        [Parameter(Mandatory)] [string]$AuthMode,
        [Parameter(Mandatory)] [string]$ReadOnly,
        [Parameter(Mandatory)] [string]$AllowDdl,
        [Parameter(Mandatory)] [string]$MaxRows,
        [Parameter(Mandatory)] [string]$QueryTimeout,
        [switch]$Force,
        [switch]$DryRun
    )

    function _info($msg) { Write-Host ('[ INFO ]  ' + $msg) -ForegroundColor Cyan }
    function _ok($msg)   { Write-Host ('[  OK  ]  ' + $msg) -ForegroundColor Green }
    function _warn($msg) { Write-Host ('[ WARN ]  ' + $msg) -ForegroundColor Yellow }
    function _fail($msg) { Write-Host ('[ FAIL ]  ' + $msg) -ForegroundColor Red; exit 1 }

    $venvPython   = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    $venvServer   = Join-Path $RepoRoot '.venv\Scripts\mssql-mcp-server.exe'
    $envFile      = Join-Path $RepoRoot 'environments.json'
    $auditPath    = Join-Path $RepoRoot 'logs\audit.jsonl'
    $tokenCache   = Join-Path $env:USERPROFILE '.cache\mssql-mcp\token-cache.bin'
    $mergeHelper  = Join-Path $RepoRoot 'scripts\merge_cowork_entry.py'

    _info ('Repo root         : ' + $RepoRoot)
    _info ('Config file       : ' + $ConfigPath)
    _info ('Server executable : ' + $venvServer)
    _info ('Environments file : ' + $envFile)

    if (-not (Test-Path $venvServer)) {
        _fail ('Server executable not found at ' + $venvServer + '. Run install.ps1 first.')
    }
    if (-not (Test-Path $venvPython)) {
        _fail ('venv Python not found at ' + $venvPython + '. Run install.ps1 first.')
    }
    if (-not (Test-Path $mergeHelper)) {
        _fail ('Merge helper missing at ' + $mergeHelper + '. Re-clone the repo.')
    }
    if (-not (Test-Path $envFile)) {
        _warn ('environments.json not found at ' + $envFile + '. Server will fall back to .env single-server mode.')
    }

    $entry = [ordered]@{
        command = $venvServer
        args    = @()
        env     = [ordered]@{
            MSSQL_AUTH_MODE          = $AuthMode
            MSSQL_READ_ONLY          = $ReadOnly
            MSSQL_ALLOW_DDL          = $AllowDdl
            MSSQL_MAX_ROWS           = $MaxRows
            MSSQL_QUERY_TIMEOUT      = $QueryTimeout
            MSSQL_ENVIRONMENTS_FILE  = $envFile
            MSSQL_AUDIT_LOG_PATH     = $auditPath
            MSSQL_TOKEN_CACHE_PATH   = $tokenCache
            MCP_TRANSPORT            = 'stdio'
            LOG_LEVEL                = 'INFO'
        }
    }
    $entryJson = $entry | ConvertTo-Json -Depth 10 -Compress

    $tempEntry = Join-Path $env:TEMP 'register-cowork-entry.json'
    [System.IO.File]::WriteAllText($tempEntry, $entryJson, [System.Text.UTF8Encoding]::new($false))

    $forceArg  = if ($Force)  { 'true' } else { 'false' }
    $dryRunArg = if ($DryRun) { 'true' } else { 'false' }

    $pyOutput = & $venvPython $mergeHelper $ConfigPath 'mssql' $tempEntry $forceArg $dryRunArg 2>&1
    $pyExit = $LASTEXITCODE

    Remove-Item $tempEntry -Force -ErrorAction SilentlyContinue

    Write-Host ''
    foreach ($line in $pyOutput) { Write-Host $line }
    Write-Host ''

    if ($pyExit -eq 2) {
        _warn 'An mssql entry already exists. Re-run with -Force to overwrite.'
        exit 1
    }
    if ($pyExit -ne 0) {
        _fail ('Merge helper failed (exit ' + $pyExit + ').')
    }

    if ($DryRun) {
        _warn 'DryRun: no changes written.'
        return
    }

    _ok ('Updated ' + $ConfigPath)
    Write-Host ''
    Write-Host 'Next steps:' -ForegroundColor White
    Write-Host '  1. Fully quit Claude Desktop (tray icon then Quit) and relaunch'
    Write-Host '  2. Open a NEW Cowork conversation'
    Write-Host '  3. Ask Claude to call server_info on the mssql server'
    Write-Host ''
    Write-Host ('If tools do not appear, check ' + $env:APPDATA + '\Claude\logs\ for mcp startup errors') -ForegroundColor Gray
}
