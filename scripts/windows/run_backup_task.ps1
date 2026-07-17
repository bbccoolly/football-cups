[CmdletBinding()]
param(
    [string]$Workspace = "",
    [ValidateSet("Incremental", "ContentAddressed")]
    [string]$Mode = "Incremental"
)

$ErrorActionPreference = "Stop"
if (-not $Workspace) {
    $Workspace = Join-Path $PSScriptRoot "..\.."
}
$workspacePath = (Resolve-Path -LiteralPath $Workspace).Path
$collector = Join-Path $workspacePath ".venv\Scripts\football-cups-collector.exe"
if (-not (Test-Path -LiteralPath $collector)) {
    throw "Collector executable not found: $collector"
}

$dataDir = Join-Path $workspacePath "data\500"
$envPath = Join-Path $workspacePath ".env"
if (Test-Path -LiteralPath $envPath) {
    foreach ($line in Get-Content -LiteralPath $envPath -Encoding UTF8) {
        if ($line -match '^\s*FOOTBALL_CUPS_DATA_DIR\s*=\s*(.*)$') {
            $value = $Matches[1].Trim().Trim('"').Trim("'")
            if ($value) {
                $dataDir = if ([System.IO.Path]::IsPathRooted($value)) {
                    [System.IO.Path]::GetFullPath($value)
                }
                else {
                    [System.IO.Path]::GetFullPath((Join-Path $workspacePath $value))
                }
            }
        }
    }
}
$logDir = Join-Path $dataDir "logs"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$logPath = Join-Path $logDir "backup-task.jsonl"
$startedAt = [DateTimeOffset]::UtcNow
$command = if ($Mode -eq "Incremental") { "backup" } else { "backup-oss" }
$raw = @(& $collector $command --workspace $workspacePath 2>&1)
$exitCode = $LASTEXITCODE
$outputText = ($raw | Out-String).Trim()
$payload = $null
try {
    $payload = $outputText | ConvertFrom-Json -ErrorAction Stop
}
catch {
    $payload = $null
}
$completedAt = [DateTimeOffset]::UtcNow
$record = [ordered]@{
    schema_version = 1
    record_type = "BackupTaskRun"
    mode = $Mode
    started_at = $startedAt.ToString("o")
    completed_at = $completedAt.ToString("o")
    exit_code = $exitCode
    status = if ($payload -and $payload.status) { [string]$payload.status } elseif ($exitCode -eq 0) { "completed" } else { "failed" }
    run_id = if ($payload -and $payload.run_id) { [string]$payload.run_id } else { $null }
    error = if ($payload -and $payload.error) { [string]$payload.error } elseif ($exitCode -ne 0) { $outputText.Substring(0, [Math]::Min(1000, $outputText.Length)) } else { $null }
}
($record | ConvertTo-Json -Compress -Depth 5) | Add-Content -LiteralPath $logPath -Encoding UTF8
if ($outputText) {
    Write-Output $outputText
}
exit $exitCode
