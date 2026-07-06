param(
    [string]$Server = $(if ($env:EVE_SENTRY_MONITOR_SERVER) { $env:EVE_SENTRY_MONITOR_SERVER } elseif ($env:EVE_SENTRY_INTEL_URL) { $env:EVE_SENTRY_INTEL_URL } else { "http://127.0.0.1:8765" }),
    [string]$Channel = $(if ($env:EVE_SENTRY_CHANNEL) { $env:EVE_SENTRY_CHANNEL } else { "" }),
    [string]$ChatlogDir = $(if ($env:EVE_SENTRY_CHATLOG_DIR) { $env:EVE_SENTRY_CHATLOG_DIR } else { "" }),
    [string]$ChannelState = $(if ($env:EVE_SENTRY_CHANNEL_STATE) { $env:EVE_SENTRY_CHANNEL_STATE } else { Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "EVE Sentry\channel_offsets.json" }),
    [string]$System = $(if ($env:EVE_SENTRY_SYSTEM) { $env:EVE_SENTRY_SYSTEM } else { "" }),
    [string]$OcrDevice = $(if ($env:EVE_SENTRY_OCR_DEVICE) { $env:EVE_SENTRY_OCR_DEVICE } else { "" }),
    [double]$HeartbeatInterval = 15.0,
    [string]$Python = "",
    [switch]$NoPublish,
    [switch]$NoEsiLocation,
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

$envUpdates = [ordered]@{
    EVE_SENTRY_INTEL_URL = $Server
    EVE_SENTRY_CHANNEL_STATE = $ChannelState
    EVE_SENTRY_HEARTBEAT_INTERVAL = [string]$HeartbeatInterval
}

if ($Channel) { $envUpdates["EVE_SENTRY_CHANNEL"] = $Channel }
if ($ChatlogDir) { $envUpdates["EVE_SENTRY_CHATLOG_DIR"] = $ChatlogDir }
if ($System) { $envUpdates["EVE_SENTRY_SYSTEM"] = $System }
if ($OcrDevice) { $envUpdates["EVE_SENTRY_OCR_DEVICE"] = $OcrDevice }
if ($NoPublish) { $envUpdates["EVE_SENTRY_PUBLISH_INTEL"] = "0" }
if ($NoEsiLocation) { $envUpdates["EVE_SENTRY_USE_ESI_LOCATION"] = "0" }

$clientArgs = @("-m", "app.detector_client")

if ($PrintCommand) {
    [PSCustomObject]@{
        python = $Python
        cwd = $repoRoot
        args = $clientArgs
        env = $envUpdates
    } | ConvertTo-Json -Depth 4
    exit 0
}

foreach ($key in $envUpdates.Keys) {
    Set-Item -Path "Env:$key" -Value $envUpdates[$key]
}

Push-Location $repoRoot
try {
    & $Python @clientArgs
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
