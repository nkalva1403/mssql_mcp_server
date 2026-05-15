<#
.SYNOPSIS
    Register (or update) the mssql MCP server in Claude Desktop's Cowork config.

.DESCRIPTION
    Thin wrapper that builds the mssql entry from this scripts knobs, then
    calls the shared Python helper at scripts\merge_cowork_entry.py to
    merge it into %APPDATA%\Claude\claude_desktop_config.json.

    install.ps1 also calls the same helper during initial setup; this
    script exists for the standalone refresh case (e.g. switching auth
    mode, re-applying after Claude Desktop wipes its config).

    Pre-req: install.ps1 must have run so the venv at .venv\Scripts
    exists. This script does not install anything.

.PARAMETER ConfigPath
    Path to claude_desktop_config.json.

.PARAMETER AuthMode
    Value for MSSQL_AUTH_MODE.

.PARAMETER ReadOnly
    Either true or false.

.PARAMETER AllowDdl
    Either true or false.

.PARAMETER Force
    Overwrite an existing mssql entry.

.PARAMETER DryRun
    Print the planned change without writing.

.EXAMPLE
    .\register-cowork.ps1
.EXAMPLE
    .\register-cowork.ps1 -AuthMode entra_cli -DryRun
.EXAMPLE
    .\register-cowork.ps1 -ReadOnly false -AllowDdl true -Force
#>

[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $env:APPDATA 'Claude\claude_desktop_config.json'),
    [ValidateSet('entra_interactive','entra_cli','entra_device_code','entra_default',
                 'entra_service_principal','entra_service_principal_cert',
                 'entra_managed_identity','sql','windows')]
    [string]$AuthMode = 'entra_interactive',
    [ValidateSet('true','false')]
    [string]$ReadOnly = 'true',
    [ValidateSet('true','false')]
    [string]$AllowDdl = 'false',
    [string]$MaxRows = '1000',
    [string]$QueryTimeout = '30',
    [switch]$Force,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

# Dot-source the shared registration function — both this script and
# install.ps1 use it, so the entry shape and Python helper invocation
# live in exactly one place.
. (Join-Path $PSScriptRoot 'scripts\register-cowork.lib.ps1')

Invoke-CoworkRegistration `
    -RepoRoot $PSScriptRoot `
    -ConfigPath $ConfigPath `
    -AuthMode $AuthMode `
    -ReadOnly $ReadOnly `
    -AllowDdl $AllowDdl `
    -MaxRows $MaxRows `
    -QueryTimeout $QueryTimeout `
    -Force:$Force `
    -DryRun:$DryRun
