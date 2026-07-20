[CmdletBinding()]
param(
    [string]$Workspace = "",
    [string]$Channel = "research-shadow-v1"
)

$ErrorActionPreference = "Stop"
if (-not $Workspace) {
    $Workspace = Join-Path $PSScriptRoot "..\.."
}
$workspacePath = (Resolve-Path -LiteralPath $Workspace).Path
$postgresScript = Join-Path $workspacePath "scripts\windows\local_postgres.ps1"
$researchCli = Join-Path $workspacePath ".venv\Scripts\football-cups-research.exe"

if (-not (Test-Path -LiteralPath $researchCli)) {
    throw "Research CLI not found: $researchCli"
}

& $postgresScript -Action Start -Workspace $workspacePath | Out-Null

$raw = @(& $researchCli shadow-predict --workspace $workspacePath --channel $Channel 2>&1)
$predictionExit = $LASTEXITCODE
if ($raw) {
    Write-Output (($raw | Out-String).Trim())
}
if ($predictionExit -ne 0) {
    exit $predictionExit
}

& $researchCli db-import --workspace $workspacePath
exit $LASTEXITCODE
