[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$Workspace = "",
    [string]$TaskName = "FootballCups-Database-Import",
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

$runner = Join-Path $workspacePath "scripts\windows\run_database_import.ps1"
if (-not (Test-Path -LiteralPath $runner)) {
    throw "Database import runner not found: $runner"
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" -Workspace `"$workspacePath`"" `
    -WorkingDirectory $workspacePath

$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5)

$userId = "$env:USERDOMAIN\$env:USERNAME"
$logonType = if ($Interactive) { "Interactive" } else { "S4U" }
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType $logonType -RunLevel Limited
$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal

if ($PSCmdlet.ShouldProcess($TaskName, "Register database import scheduled task")) {
    try {
        Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
    }
    catch [Microsoft.Management.Infrastructure.CimException] {
        if (-not $Interactive -and $_.Exception.Message -match "Access is denied") {
            throw "S4U task registration requires an elevated PowerShell. Rerun with -Interactive for logged-in validation."
        }
        throw
    }
}
