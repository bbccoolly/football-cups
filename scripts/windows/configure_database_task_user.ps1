[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$Workspace = "",
    [string]$UserName = "football-cups-runner",
    [string]$TaskName = "FootballCups-Database-Import",
    [string]$ShadowTaskName = "FootballCups-Research-Shadow-Prediction",
    [switch]$SkipTaskRegistration
)

$ErrorActionPreference = "Stop"
if (-not $Workspace) {
    $Workspace = Join-Path $PSScriptRoot "..\.."
}
$workspacePath = (Resolve-Path -LiteralPath $Workspace).Path
$identity = "$env:COMPUTERNAME\$UserName"
$taskInstaller = Join-Path $workspacePath "scripts\windows\install_database_import_task.ps1"
$shadowTaskInstaller = Join-Path $workspacePath "scripts\windows\install_shadow_prediction_task.ps1"
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

$usersGroup = Get-LocalGroup -SID "S-1-5-32-545" -ErrorAction Stop
$users = @(Get-LocalGroupMember -Group $usersGroup -ErrorAction Stop)
if ($user -and $users.SID -notcontains $user.SID -and
    $PSCmdlet.ShouldProcess($identity, "Add task user to the built-in Users group")) {
    Add-LocalGroupMember -Group $usersGroup -Member $user
}

$administratorGroup = Get-LocalGroup -SID "S-1-5-32-544" -ErrorAction Stop
$administrators = @(Get-LocalGroupMember -Group $administratorGroup -ErrorAction Stop)
if ($administrators.Name -contains $identity) {
    throw "Database task user must not be a member of Administrators: $identity"
}

function Grant-DirectoryAccess(
    [string]$Path,
    [Security.Principal.IdentityReference]$Principal,
    [Security.AccessControl.FileSystemRights]$Rights,
    [Security.AccessControl.InheritanceFlags]$InheritanceFlags
) {
    $acl = Get-Acl -LiteralPath $Path
    $principalSid = $Principal.Translate([Security.Principal.SecurityIdentifier]).Value
    $matchingRule = $acl.Access | Where-Object {
        try {
            $ruleSid = $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        }
        catch {
            $ruleSid = ""
        }
        $ruleSid -eq $principalSid -and
            $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
            ($_.FileSystemRights -band $Rights) -eq $Rights -and
            $_.InheritanceFlags -eq $InheritanceFlags
    } | Select-Object -First 1
    if ($matchingRule) {
        return
    }
    $rule = [Security.AccessControl.FileSystemAccessRule]::new(
        $Principal,
        $Rights,
        $InheritanceFlags,
        [Security.AccessControl.PropagationFlags]::None,
        [Security.AccessControl.AccessControlType]::Allow
    )
    [void]$acl.AddAccessRule($rule)
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function New-RandomPassword {
    $groups = @(
        'abcdefghijkmnopqrstuvwxyz',
        'ABCDEFGHJKLMNPQRSTUVWXYZ',
        '23456789',
        '!@#$%^&*_-+='
    )
    $alphabet = $groups -join ''
    $bytes = New-Object byte[] 64
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    }
    finally {
        $rng.Dispose()
    }
    $characters = New-Object Collections.Generic.List[char]
    for ($index = 0; $index -lt $groups.Count; $index++) {
        $group = $groups[$index]
        $characters.Add($group[$bytes[$index] % $group.Length])
    }
    for ($index = $groups.Count; $index -lt 32; $index++) {
        $characters.Add($alphabet[$bytes[$index] % $alphabet.Length])
    }
    for ($index = $characters.Count - 1; $index -gt 0; $index--) {
        $swapIndex = $bytes[32 + ($characters.Count - 1 - $index)] % ($index + 1)
        $temporary = $characters[$index]
        $characters[$index] = $characters[$swapIndex]
        $characters[$swapIndex] = $temporary
    }
    return -join $characters
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
    if (-not $user) {
        throw "Database task user was not created: $identity"
    }
    foreach ($path in $readPaths) {
        if (Test-Path -LiteralPath $path) {
            Grant-DirectoryAccess `
                -Path $path `
                -Principal $user.SID `
                -Rights ReadAndExecute `
                -InheritanceFlags "ContainerInherit, ObjectInherit"
        }
    }
    Grant-DirectoryAccess `
        -Path $dataPath `
        -Principal $user.SID `
        -Rights Modify `
        -InheritanceFlags "ContainerInherit, ObjectInherit"
    $envPath = Join-Path $workspacePath ".env"
    if (Test-Path -LiteralPath $envPath) {
        $envAcl = Get-Acl -LiteralPath $envPath
        $envRuleExists = $envAcl.Access | Where-Object {
            try {
                $ruleSid = $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
            }
            catch {
                $ruleSid = ""
            }
            $ruleSid -eq $user.SID.Value -and
                $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
                ($_.FileSystemRights -band [Security.AccessControl.FileSystemRights]::Read) -eq
                    [Security.AccessControl.FileSystemRights]::Read
        } | Select-Object -First 1
        if (-not $envRuleExists) {
            $envRule = [Security.AccessControl.FileSystemAccessRule]::new(
                $user.SID,
                [Security.AccessControl.FileSystemRights]::Read,
                [Security.AccessControl.AccessControlType]::Allow
            )
            [void]$envAcl.AddAccessRule($envRule)
            Set-Acl -LiteralPath $envPath -AclObject $envAcl
        }
    }

    $venvConfig = Join-Path $workspacePath ".venv\pyvenv.cfg"
    if (-not (Test-Path -LiteralPath $venvConfig)) {
        throw "Virtual environment config not found: $venvConfig"
    }
    $baseExecutableLine = Get-Content -LiteralPath $venvConfig |
        Where-Object { $_ -match "^base-executable\s*=\s*(.+)$" } |
        Select-Object -First 1
    if (-not $baseExecutableLine) {
        throw "base-executable is missing from: $venvConfig"
    }
    [void]($baseExecutableLine -match "^base-executable\s*=\s*(.+)$")
    $baseExecutable = $Matches[1].Trim()
    if (-not (Test-Path -LiteralPath $baseExecutable)) {
        throw "Base Python executable not found: $baseExecutable"
    }
    $pythonRuntime = Split-Path -Parent $baseExecutable
    $root = [IO.Path]::GetPathRoot($pythonRuntime).TrimEnd('\')
    $current = $pythonRuntime
    while ($current -and $current.TrimEnd('\') -ne $root) {
        $inheritance = if ($current -eq $pythonRuntime) {
            [Security.AccessControl.InheritanceFlags]"ContainerInherit, ObjectInherit"
        }
        else {
            [Security.AccessControl.InheritanceFlags]::None
        }
        Grant-DirectoryAccess `
            -Path $current `
            -Principal $user.SID `
            -Rights ReadAndExecute `
            -InheritanceFlags $inheritance
        $current = Split-Path -Parent $current
    }
}

$taskRegistered = $false
$shadowTaskRegistered = $false
$taskLogonType = "NotRegistered"
if (-not $SkipTaskRegistration -and
    $PSCmdlet.ShouldProcess($identity, "Rotate the local task password and register database plus shadow tasks")) {
    if (-not (Test-Path -LiteralPath $taskInstaller)) {
        throw "Database task installer not found: $taskInstaller"
    }
    if (-not (Test-Path -LiteralPath $shadowTaskInstaller)) {
        throw "Shadow prediction task installer not found: $shadowTaskInstaller"
    }
    $passwordText = New-RandomPassword
    $password = ConvertTo-SecureString $passwordText -AsPlainText -Force
    try {
        Set-LocalUser `
            -Name $UserName `
            -Password $password `
            -PasswordNeverExpires $true `
            -UserMayChangePassword $false
        & net.exe user $UserName /passwordreq:yes | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to require a password for the database task user"
        }
        & $taskInstaller `
            -Workspace $workspacePath `
            -TaskName $TaskName `
            -UserId $identity `
            -PasswordLogon `
            -Password $password
        $taskRegistered = $true
        & $shadowTaskInstaller `
            -Workspace $workspacePath `
            -TaskName $ShadowTaskName `
            -UserId $identity `
            -PasswordLogon `
            -Password $password
        $shadowTaskRegistered = $true
        $taskLogonType = "Password"
    }
    finally {
        $passwordText = $null
        $password = $null
        [GC]::Collect()
    }
}

[pscustomobject]@{
    UserId = $identity
    Enabled = if ($user) { $user.Enabled } else { $false }
    Administrator = $false
    WorkspacePermission = "ReadAndExecute"
    DataPermission = "Modify"
    TaskName = $TaskName
    TaskRegistered = $taskRegistered
    ShadowTaskName = $ShadowTaskName
    ShadowTaskRegistered = $shadowTaskRegistered
    TaskLogonType = $taskLogonType
}
