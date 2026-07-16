[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateSet("Install", "Start", "Stop", "Status")]
    [string]$Action = "Install",
    [string]$Workspace = "",
    [int]$Port = 55432
)

$ErrorActionPreference = "Stop"
$version = "17.10-2"
$archiveName = "postgresql-$version-windows-x64-binaries.zip"
$downloadUrl = "https://get.enterprisedb.com/postgresql/$archiveName"
$expectedSha256 = "EF9B1E5E23D2E8A83914BA13D9DC536A72210FBA53FD1808FF1F7E06BB22B106"

if (-not $Workspace) {
    $Workspace = Join-Path $PSScriptRoot "..\.."
}
$workspacePath = (Resolve-Path -LiteralPath $Workspace).Path
$dataRoot = Join-Path $workspacePath "data"
$downloadDir = Join-Path $dataRoot "runtime\downloads"
$archivePath = Join-Path $downloadDir $archiveName
$runtimeRoot = Join-Path $dataRoot "runtime\postgresql\$version"
$binDir = Join-Path $runtimeRoot "pgsql\bin"
$clusterDir = Join-Path $dataRoot "postgresql\17-main"

function Assert-WorkspacePath([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    $prefix = $workspacePath.TrimEnd('\') + '\'
    if (-not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing path outside workspace: $full"
    }
}

function Get-PgTool([string]$Name) {
    $path = Join-Path $binDir "$Name.exe"
    if (-not (Test-Path -LiteralPath $path)) {
        throw "PostgreSQL tool not installed: $path"
    }
    return $path
}

function Get-ServerStatus {
    $pgCtl = Get-PgTool "pg_ctl"
    & $pgCtl status -D $clusterDir *> $null
    return $LASTEXITCODE -eq 0
}

function Start-LocalServer {
    if (Get-ServerStatus) {
        return
    }
    $postgres = Get-PgTool "postgres"
    if ($PSCmdlet.ShouldProcess($clusterDir, "Start local PostgreSQL")) {
        $stdout = Join-Path $clusterDir "server.stdout.log"
        $stderr = Join-Path $clusterDir "server.stderr.log"
        $arguments = @("-D", "`"$clusterDir`"")
        Start-Process `
            -FilePath $postgres `
            -ArgumentList $arguments `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdout `
            -RedirectStandardError $stderr
        $deadline = (Get-Date).AddSeconds(60)
        do {
            Start-Sleep -Milliseconds 500
            if (Get-ServerStatus) {
                break
            }
        } while ((Get-Date) -lt $deadline)
        if (-not (Get-ServerStatus)) {
            $message = [string](Get-Content -LiteralPath $stderr -Raw -ErrorAction SilentlyContinue)
            throw "PostgreSQL failed to start: $($message.Trim())"
        }
    }
}

function Install-Runtime {
    New-Item -ItemType Directory -Path $downloadDir -Force | Out-Null
    if (-not (Test-Path -LiteralPath $archivePath)) {
        if ($PSCmdlet.ShouldProcess($archivePath, "Download PostgreSQL $version binaries")) {
            Invoke-WebRequest -UseBasicParsing -Uri $downloadUrl -OutFile $archivePath
        }
    }
    if (-not (Test-Path -LiteralPath $archivePath)) {
        throw "PostgreSQL archive is unavailable: $archivePath"
    }
    $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $archivePath).Hash
    if ($actualHash -ne $expectedSha256) {
        throw "PostgreSQL archive SHA-256 mismatch"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $binDir "postgres.exe"))) {
        $extracting = Join-Path $dataRoot "runtime\postgresql\.extracting-$version"
        Assert-WorkspacePath $extracting
        Assert-WorkspacePath $runtimeRoot
        if (Test-Path -LiteralPath $extracting) {
            Remove-Item -LiteralPath $extracting -Recurse -Force
        }
        New-Item -ItemType Directory -Path $extracting -Force | Out-Null
        if ($PSCmdlet.ShouldProcess($runtimeRoot, "Extract PostgreSQL $version binaries")) {
            Expand-Archive -LiteralPath $archivePath -DestinationPath $extracting
            if (Test-Path -LiteralPath $runtimeRoot) {
                Remove-Item -LiteralPath $runtimeRoot -Recurse -Force
            }
            Move-Item -LiteralPath $extracting -Destination $runtimeRoot
        }
    }
}

function Initialize-Cluster {
    if (Test-Path -LiteralPath (Join-Path $clusterDir "PG_VERSION")) {
        return
    }
    $initdb = Get-PgTool "initdb"
    New-Item -ItemType Directory -Path $clusterDir -Force | Out-Null
    if ($PSCmdlet.ShouldProcess($clusterDir, "Initialize local PostgreSQL cluster")) {
        & $initdb -D $clusterDir -U football_cups -A trust --encoding=UTF8 --locale=C
        if ($LASTEXITCODE -ne 0) {
            throw "initdb failed"
        }
        Add-Content -LiteralPath (Join-Path $clusterDir "postgresql.conf") -Encoding UTF8 -Value @"

# Football Cups local analysis database
listen_addresses = '127.0.0.1'
port = $Port
timezone = 'UTC'
log_timezone = 'UTC'
logging_collector = on
log_directory = 'log'
log_filename = 'postgresql-%d.log'
log_truncate_on_rotation = on
log_rotation_age = '1d'
"@
    }
}

function Ensure-Database([string]$DatabaseName) {
    $psql = Get-PgTool "psql"
    $createdb = Get-PgTool "createdb"
    $exists = & $psql -h 127.0.0.1 -p $Port -U football_cups -d postgres -tAc `
        "SELECT 1 FROM pg_database WHERE datname = '$DatabaseName'"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to inspect PostgreSQL databases"
    }
    if (-not $exists) {
        if ($PSCmdlet.ShouldProcess($DatabaseName, "Create local PostgreSQL database")) {
            & $createdb -h 127.0.0.1 -p $Port -U football_cups $DatabaseName
            if ($LASTEXITCODE -ne 0) {
                throw "Failed to create database: $DatabaseName"
            }
        }
    }
}

Assert-WorkspacePath $dataRoot
Assert-WorkspacePath $runtimeRoot
Assert-WorkspacePath $clusterDir

if ($WhatIfPreference) {
    [pscustomobject]@{
        Action = $Action
        Version = $version
        Port = $Port
        Runtime = $runtimeRoot
        Data = $clusterDir
        WhatIf = $true
    } | ConvertTo-Json
    return
}

switch ($Action) {
    "Install" {
        Install-Runtime
        Initialize-Cluster
        Start-LocalServer
        Ensure-Database "football_cups"
        Ensure-Database "football_cups_test"
    }
    "Start" {
        Start-LocalServer
    }
    "Stop" {
        if (Get-ServerStatus) {
            $pgCtl = Get-PgTool "pg_ctl"
            if ($PSCmdlet.ShouldProcess($clusterDir, "Stop local PostgreSQL")) {
                & $pgCtl stop -D $clusterDir -m fast -w -t 60
                if ($LASTEXITCODE -ne 0) {
                    throw "PostgreSQL failed to stop"
                }
            }
        }
    }
    "Status" {
        # Status is emitted below.
    }
}

$running = $false
if ((Test-Path -LiteralPath (Join-Path $binDir "pg_ctl.exe")) -and
    (Test-Path -LiteralPath (Join-Path $clusterDir "PG_VERSION"))) {
    $running = Get-ServerStatus
}
[pscustomobject]@{
    Version = $version
    Port = $Port
    Runtime = $runtimeRoot
    Data = $clusterDir
    Running = $running
} | ConvertTo-Json
