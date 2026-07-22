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
$workflowPath = Join-Path $workspacePath "config\research-k1-analysis-workflow.json"
$evaluationStatePath = Join-Path $workspacePath "data\research\state\k1-forward-evaluation-schedule.json"

if (-not (Test-Path -LiteralPath $researchCli)) {
    throw "Research CLI not found: $researchCli"
}

function Get-K1EvaluationDue {
    if (-not (Test-Path -LiteralPath $workflowPath)) {
        throw "K1 analysis workflow config not found: $workflowPath"
    }
    try {
        $workflow = Get-Content -LiteralPath $workflowPath -Raw | ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw "K1 analysis workflow config is invalid: $($_.Exception.Message)"
    }
    if (-not $workflow.daily_evaluation.enabled) {
        return $null
    }
    if ($workflow.daily_evaluation.timezone -ne "Asia/Shanghai") {
        throw "Only the registered Asia/Shanghai K1 evaluation timezone is supported."
    }
    if ($workflow.daily_evaluation.local_time -notmatch '^([01]\d|2[0-3]):[0-5]\d$') {
        throw "K1 evaluation local_time must be HH:MM."
    }
    $now = [DateTimeOffset]::UtcNow.ToOffset([TimeSpan]::FromHours(8))
    $dueTime = [TimeSpan]::ParseExact($workflow.daily_evaluation.local_time, 'hh\:mm', $null)
    if ($now.TimeOfDay -lt $dueTime) {
        return $null
    }
    $localDate = $now.ToString('yyyy-MM-dd')
    if (Test-Path -LiteralPath $evaluationStatePath) {
        try {
            $state = Get-Content -LiteralPath $evaluationStatePath -Raw | ConvertFrom-Json -ErrorAction Stop
        } catch {
            throw "K1 evaluation state is invalid: $($_.Exception.Message)"
        }
        if ($state.schema_version -ne 1 -or [string]::IsNullOrWhiteSpace([string]$state.local_date)) {
            throw "K1 evaluation state is structurally invalid."
        }
        if ($state.local_date -eq $localDate) {
            return $null
        }
    }
    return [pscustomobject]@{ LocalDate = $localDate; CheckedAt = $now.ToUniversalTime().ToString('o') }
}

function Complete-K1EvaluationState {
    param(
        [Parameter(Mandatory)]$Due,
        [Parameter(Mandatory)]$Evaluation
    )
    $directory = Split-Path -Parent $evaluationStatePath
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    $payload = [ordered]@{
        schema_version = 1
        local_date = $Due.LocalDate
        checked_at = $Due.CheckedAt
        evaluation_status = [string]$Evaluation.status
        automatic_evidence_set_hash = $Evaluation.automatic_evidence_set_hash
        evaluation_record_id = $Evaluation.evaluation_record_id
    } | ConvertTo-Json -Depth 4
    $temporary = "$evaluationStatePath.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        [System.IO.File]::WriteAllText($temporary, $payload + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporary -Destination $evaluationStatePath -Force
    } finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
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

$europeRaw = @(& $researchCli europe-guardrail-shadow --workspace $workspacePath --channel "research-europe-guardrail-v1" 2>&1)
$europeExit = $LASTEXITCODE
if ($europeRaw) {
    Write-Output (($europeRaw | Out-String).Trim())
}
if ($europeExit -ne 0) {
    exit $europeExit
}

& $researchCli db-import --workspace $workspacePath
$importExit = $LASTEXITCODE
if ($importExit -ne 0) {
    exit $importExit
}

$due = Get-K1EvaluationDue
if ($null -eq $due) {
    exit 0
}

$evaluationRaw = @(& $researchCli evaluate-k1-guardrail-forward --workspace $workspacePath --channel $Channel 2>&1)
$evaluationExit = $LASTEXITCODE
if ($evaluationRaw) {
    Write-Output (($evaluationRaw | Out-String).Trim())
}
if ($evaluationExit -ne 0) {
    exit $evaluationExit
}
try {
    $evaluation = (($evaluationRaw | Out-String) | ConvertFrom-Json -ErrorAction Stop)
} catch {
    throw "K1 evaluation output is invalid: $($_.Exception.Message)"
}
if ($evaluation.status -notin @('completed', 'unchanged')) {
    throw "K1 evaluation returned unexpected status: $($evaluation.status)"
}

& $researchCli db-import --workspace $workspacePath
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
Complete-K1EvaluationState -Due $due -Evaluation $evaluation
exit 0
