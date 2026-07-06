param(
    [string]$Server = $(if ($env:EVE_SENTRY_ALERT_SERVER) { $env:EVE_SENTRY_ALERT_SERVER } elseif ($env:EVE_SENTRY_SERVER) { $env:EVE_SENTRY_SERVER } else { "http://127.0.0.1:8765" }),
    [string]$State = $(if ($env:EVE_SENTRY_ALERT_STATE) { $env:EVE_SENTRY_ALERT_STATE } else { Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "EVE Sentry\alert_client_state.json" }),
    [double]$Interval = 2.0,
    [int]$Limit = 50,
    [double]$Timeout = 3.0,
    [double]$StreamRetryInterval = 30.0,
    [string]$MinLevel = "",
    [int]$MinScore = -1,
    [string]$AckBy = "alert-client",
    [string]$AckNote = "",
    [string]$Python = "",
    [switch]$NoPopup,
    [switch]$NoDetails,
    [switch]$Ack,
    [switch]$Poll,
    [switch]$Once,
    [switch]$Json,
    [switch]$IncludeExisting,
    [switch]$UnacknowledgedOnly,
    [switch]$NoState,
    [switch]$PrintCommand
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $Python) {
    $venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
    if (Test-Path $venvPython) {
        $Python = $venvPython
    } else {
        $Python = "python"
    }
}

$clientArgs = @(
    "-m", "app.alert_client",
    "--server", $Server,
    "--state", $State,
    "--interval", [string]$Interval,
    "--limit", [string]$Limit,
    "--timeout", [string]$Timeout,
    "--stream-retry-interval", [string]$StreamRetryInterval
)

if (-not $NoPopup) { $clientArgs += "--popup" }
if (-not $NoDetails) { $clientArgs += "--details" }
if ($Poll) { $clientArgs += "--poll" }
if ($Once) { $clientArgs += "--once" }
if ($Json) { $clientArgs += "--json" }
if ($IncludeExisting) { $clientArgs += "--include-existing" }
if ($UnacknowledgedOnly) { $clientArgs += "--unacknowledged-only" }
if ($NoState) { $clientArgs += "--no-state" }
if ($MinLevel) { $clientArgs += @("--min-level", $MinLevel) }
if ($MinScore -ge 0) { $clientArgs += @("--min-score", [string]$MinScore) }
if ($Ack) {
    $clientArgs += "--ack"
    $clientArgs += @("--ack-by", $AckBy)
    if ($AckNote) { $clientArgs += @("--ack-note", $AckNote) }
}

if ($PrintCommand) {
    [PSCustomObject]@{
        python = $Python
        cwd = $repoRoot
        args = $clientArgs
    } | ConvertTo-Json -Depth 4
    exit 0
}

Push-Location $repoRoot
try {
    & $Python @clientArgs
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
