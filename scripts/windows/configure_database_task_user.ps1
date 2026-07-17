[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$Workspace = "",
    [string]$UserName = "football-cups-runner"
)

$ErrorActionPreference = "Stop"
if (-not $Workspace) {
    $Workspace = Join-Path $PSScriptRoot "..\.."
}
$workspacePath = (Resolve-Path -LiteralPath $Workspace).Path
$identity = "$env:COMPUTERNAME\$UserName"
$currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]$currentIdentity
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Database task user configuration requires an elevated PowerShell."
}

$user = Get-LocalUser -Name $UserName -ErrorAction SilentlyContinue
if (-not $user -and $PSCmdlet.ShouldProcess($identity, "Create non-administrator S4U task user")) {
    New-LocalUser `
        -Name $UserName `
        -NoPassword `
        -AccountNeverExpires `
        -UserMayNotChangePassword `
        -Description "Football Cups PostgreSQL import task runner" | Out-Null
    $user = Get-LocalUser -Name $UserName -ErrorAction Stop
}
if ($user -and -not $user.Enabled -and $PSCmdlet.ShouldProcess($identity, "Enable task user")) {
    Enable-LocalUser -Name $UserName
}

$administratorGroup = Get-LocalGroup -SID "S-1-5-32-544" -ErrorAction Stop
$administrators = @(Get-LocalGroupMember -Group $administratorGroup -ErrorAction Stop)
if ($administrators.Name -contains $identity) {
    throw "Database task user must not be a member of Administrators: $identity"
}

$readPaths = @(
    $workspacePath,
    (Join-Path $workspacePath ".venv"),
    (Join-Path $workspacePath "config"),
    (Join-Path $workspacePath "scripts"),
    (Join-Path $workspacePath "src")
)
$dataPath = Join-Path $workspacePath "data"
if ($PSCmdlet.ShouldProcess($identity, "Grant least-privilege project and data ACLs")) {
    foreach ($path in $readPaths) {
        if (Test-Path -LiteralPath $path) {
            & icacls.exe $path /grant "${identity}:(OI)(CI)RX" /T /C | Out-Null
            if ($LASTEXITCODE -ne 0) {
                throw "Failed to grant read/execute permission: $path"
            }
        }
    }
    & icacls.exe $dataPath /grant "${identity}:(OI)(CI)M" /T /C | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to grant modify permission: $dataPath"
    }
    $envPath = Join-Path $workspacePath ".env"
    if (Test-Path -LiteralPath $envPath) {
        & icacls.exe $envPath /grant "${identity}:R" /C | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to grant environment-file read permission"
        }
    }
}

[pscustomobject]@{
    UserId = $identity
    Enabled = if ($user) { $user.Enabled } else { $false }
    Administrator = $false
    WorkspacePermission = "ReadAndExecute"
    DataPermission = "Modify"
}
