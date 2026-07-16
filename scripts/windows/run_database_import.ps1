[CmdletBinding()]
param(
    [string]$Workspace = ""
)

$ErrorActionPreference = "Stop"
if (-not $Workspace) {
    $Workspace = Join-Path $PSScriptRoot "..\.."
}
$workspacePath = (Resolve-Path -LiteralPath $Workspace).Path
$postgresScript = Join-Path $workspacePath "scripts\windows\local_postgres.ps1"
$databaseCli = Join-Path $workspacePath ".venv\Scripts\football-cups-db.exe"

if (-not (Test-Path -LiteralPath $databaseCli)) {
    throw "Database CLI not found: $databaseCli"
}

& $postgresScript -Action Start -Workspace $workspacePath | Out-Null

& $databaseCli import-files --workspace $workspacePath
exit $LASTEXITCODE
