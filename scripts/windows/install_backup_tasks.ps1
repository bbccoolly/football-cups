[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$Workspace = "",
    [string]$DailyTaskName = "FootballCups-Daily-Backup",
    [string]$WeeklyTaskName = "FootballCups-Weekly-Verified-Backup",
    [string]$UserId = "",
    [switch]$Interactive,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
if (-not $Workspace) {
    $Workspace = Join-Path $PSScriptRoot "..\.."
}
$workspacePath = (Resolve-Path -LiteralPath $Workspace).Path
$runner = Join-Path $workspacePath "scripts\windows\run_backup_task.ps1"
if (-not (Test-Path -LiteralPath $runner)) {
    throw "Backup task runner not found: $runner"
}

$taskNames = @($DailyTaskName, $WeeklyTaskName)
if ($Uninstall) {
    foreach ($taskName in $taskNames) {
        if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
            if ($PSCmdlet.ShouldProcess($taskName, "Unregister scheduled task")) {
                Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
            }
        }
    }
    return
}

if (-not $UserId) {
    $UserId = "$env:USERDOMAIN\$env:USERNAME"
}
$logonType = if ($Interactive) { "Interactive" } else { "S4U" }
$principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType $logonType -RunLevel Limited

function New-BackupTaskSettings {
    param([int]$Hours)
    return New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -WakeToRun `
        -ExecutionTimeLimit (New-TimeSpan -Hours $Hours) `
        -MultipleInstances IgnoreNew `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 5)
}

$definitions = @(
    @{
        Name = $DailyTaskName
        Mode = "Incremental"
        Trigger = New-ScheduledTaskTrigger -Daily -At "03:30"
        Settings = New-BackupTaskSettings -Hours 4
    },
    @{
        Name = $WeeklyTaskName
        Mode = "ContentAddressed"
        Trigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Sunday -At "04:30"
        Settings = New-BackupTaskSettings -Hours 6
    }
)

foreach ($definition in $definitions) {
    $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" -Workspace `"$workspacePath`" -Mode $($definition.Mode)"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments -WorkingDirectory $workspacePath
    $task = New-ScheduledTask `
        -Action $action `
        -Trigger $definition.Trigger `
        -Settings $definition.Settings `
        -Principal $principal
    if ($PSCmdlet.ShouldProcess($definition.Name, "Register $($definition.Mode) backup scheduled task")) {
        try {
            Register-ScheduledTask -TaskName $definition.Name -InputObject $task -Force | Out-Null
        }
        catch [Microsoft.Management.Infrastructure.CimException] {
            if (-not $Interactive -and $_.Exception.Message -match "Access is denied") {
                throw "S4U task registration requires an elevated PowerShell."
            }
            throw
        }
    }
}
