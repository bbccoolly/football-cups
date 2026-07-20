[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$Workspace = "",
    [string]$TaskName = "FootballCups-Research-Shadow-Prediction",
    [string]$Channel = "research-shadow-v1",
    [string]$UserId = "",
    [Security.SecureString]$Password,
    [switch]$PasswordLogon,
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

$runner = Join-Path $workspacePath "scripts\windows\run_shadow_prediction.ps1"
if (-not (Test-Path -LiteralPath $runner)) {
    throw "Shadow prediction runner not found: $runner"
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" -Workspace `"$workspacePath`" -Channel `"$Channel`"" `
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
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5)

if (-not $UserId) {
    $UserId = "$env:USERDOMAIN\$env:USERNAME"
}
if ($Interactive -and $PasswordLogon) {
    throw "Interactive and PasswordLogon cannot be used together."
}
if ($PasswordLogon -and -not $Password) {
    throw "PasswordLogon requires an in-memory SecureString password."
}
$logonType = if ($Interactive) { "Interactive" } elseif ($PasswordLogon) { "Password" } else { "S4U" }
$principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType $logonType -RunLevel Limited
$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal

if ($PSCmdlet.ShouldProcess($TaskName, "Register research shadow prediction scheduled task")) {
    $passwordText = $null
    try {
        if ($PasswordLogon) {
            $credential = [Management.Automation.PSCredential]::new($UserId, $Password)
            $passwordText = $credential.GetNetworkCredential().Password
            Register-ScheduledTask `
                -TaskName $TaskName `
                -InputObject $task `
                -User $UserId `
                -Password $passwordText `
                -Force | Out-Null
        }
        else {
            Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
        }
    }
    finally {
        $passwordText = $null
        $credential = $null
    }
}
