[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$Workspace = "",
    [string]$CollectorExecutable = "",
    [string]$TaskName = "FootballCups-500-Collector",
    [switch]$Interactive,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
if (-not $Workspace) {
    $Workspace = Join-Path $PSScriptRoot "..\.."
}
$workspacePath = (Resolve-Path -LiteralPath $Workspace).Path

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        if ($PSCmdlet.ShouldProcess($TaskName, "Unregister scheduled task")) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        }
    }
    return
}

if (-not $CollectorExecutable) {
    $CollectorExecutable = Join-Path $workspacePath ".venv\Scripts\football-cups-collector.exe"
}
if (-not (Test-Path -LiteralPath $CollectorExecutable)) {
    throw "Collector executable not found: $CollectorExecutable. Create .venv and run pip install -e .[dev] first."
}
$collectorPath = (Resolve-Path -LiteralPath $CollectorExecutable).Path

$action = New-ScheduledTaskAction `
    -Execute $collectorPath `
    -Argument "run-once --workspace `"$workspacePath`"" `
    -WorkingDirectory $workspacePath

$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 2) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5)

$userId = "$env:USERDOMAIN\$env:USERNAME"
$logonType = if ($Interactive) { "Interactive" } else { "S4U" }
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType $logonType -RunLevel Limited
$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal

if ($PSCmdlet.ShouldProcess($TaskName, "Register 500 collector scheduled task")) {
    try {
        Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
    }
    catch [Microsoft.Management.Infrastructure.CimException] {
        if (-not $Interactive -and $_.Exception.Message -match "Access is denied") {
            throw "S4U task registration requires an elevated PowerShell. For the 24-hour logged-in validation only, rerun with -Interactive."
        }
        throw
    }
}
