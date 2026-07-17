[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$Workspace = "",
    [Parameter(Mandatory = $true)]
    [string]$BackupDir,
    [Parameter(Mandatory = $true)]
    [string]$OssBackupDir
)

$ErrorActionPreference = "Stop"
if (-not $Workspace) {
    $Workspace = Join-Path $PSScriptRoot "..\.."
}
$workspacePath = (Resolve-Path -LiteralPath $Workspace).Path
$envPath = Join-Path $workspacePath ".env"

function Resolve-ConfiguredPath {
    param([string]$Value)
    if ([System.IO.Path]::IsPathRooted($Value)) {
        return [System.IO.Path]::GetFullPath($Value)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $workspacePath $Value))
}

function Get-EnvValue {
    param([string]$Name)
    if (-not (Test-Path -LiteralPath $envPath)) {
        return ""
    }
    foreach ($line in Get-Content -LiteralPath $envPath -Encoding UTF8) {
        if ($line -match "^\s*$([regex]::Escape($Name))\s*=\s*(.*)$") {
            return $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
    return ""
}

function Get-DiskNumberForPath {
    param([string]$Path)
    $root = [System.IO.Path]::GetPathRoot($Path)
    if (-not $root -or $root.StartsWith("\\")) {
        return $null
    }
    $letter = $root.Substring(0, 1)
    return (Get-Partition -DriveLetter $letter -ErrorAction Stop).DiskNumber
}

$dataValue = Get-EnvValue "FOOTBALL_CUPS_DATA_DIR"
$dataDir = if ($dataValue) {
    Resolve-ConfiguredPath $dataValue
}
else {
    Join-Path $workspacePath "data\500"
}
$backupPath = Resolve-ConfiguredPath $BackupDir
$ossBackupPath = Resolve-ConfiguredPath $OssBackupDir
$dataDisk = Get-DiskNumberForPath $dataDir
$backupDisk = Get-DiskNumberForPath $backupPath
$ossDisk = Get-DiskNumberForPath $ossBackupPath

if ($null -eq $dataDisk -or $null -eq $backupDisk -or $null -eq $ossDisk) {
    throw "Local backup configuration requires drive-letter paths with discoverable physical disks."
}
if ($dataDisk -eq $backupDisk -or $dataDisk -eq $ossDisk) {
    throw "Backup destinations must be on a different physical disk from the collector data."
}

if ($PSCmdlet.ShouldProcess("$backupPath and $ossBackupPath", "Create backup directories")) {
    New-Item -ItemType Directory -Path $backupPath -Force | Out-Null
    New-Item -ItemType Directory -Path $ossBackupPath -Force | Out-Null
}

$updates = [ordered]@{
    FOOTBALL_CUPS_BACKUP_DIR = $backupPath
    FOOTBALL_CUPS_OSS_BACKUP_DIR = $ossBackupPath
}
$existing = New-Object 'System.Collections.Generic.List[string]'
if (Test-Path -LiteralPath $envPath) {
    foreach ($line in Get-Content -LiteralPath $envPath -Encoding UTF8) {
        $existing.Add($line)
    }
}
foreach ($name in $updates.Keys) {
    $pattern = "^\s*$([regex]::Escape($name))\s*="
    for ($index = $existing.Count - 1; $index -ge 0; $index--) {
        if ($existing[$index] -match $pattern) {
            $existing.RemoveAt($index)
        }
    }
    $existing.Add("$name=$($updates[$name])")
}

if ($PSCmdlet.ShouldProcess($envPath, "Atomically update backup environment settings")) {
    $tempPath = Join-Path $workspacePath ".env.$([guid]::NewGuid().ToString('N')).tmp"
    $replaceBackupPath = Join-Path $workspacePath ".env.$([guid]::NewGuid().ToString('N')).replace-backup.tmp"
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($tempPath, (($existing -join [Environment]::NewLine) + [Environment]::NewLine), $encoding)
    try {
        if (Test-Path -LiteralPath $envPath) {
            [System.IO.File]::Replace($tempPath, $envPath, $replaceBackupPath, $true)
        }
        else {
            Move-Item -LiteralPath $tempPath -Destination $envPath
        }
    }
    finally {
        Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $replaceBackupPath -Force -ErrorAction SilentlyContinue
    }
}

[pscustomobject]@{
    Workspace = $workspacePath
    DataDirectory = $dataDir
    DataDiskNumber = $dataDisk
    BackupDirectory = $backupPath
    BackupDiskNumber = $backupDisk
    OssBackupDirectory = $ossBackupPath
    OssBackupDiskNumber = $ossDisk
}
