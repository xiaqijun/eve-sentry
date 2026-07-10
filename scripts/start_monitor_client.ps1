param(
    [string]$Server = $(if ($env:EVE_SENTRY_MONITOR_SERVER) { $env:EVE_SENTRY_MONITOR_SERVER } elseif ($env:EVE_SENTRY_INTEL_URL) { $env:EVE_SENTRY_INTEL_URL } else { "http://127.0.0.1:8765" }),
    [string]$Channel = $(if ($env:EVE_SENTRY_CHANNEL) { $env:EVE_SENTRY_CHANNEL } else { "" }),
    [string]$ChatlogDir = $(if ($env:EVE_SENTRY_CHATLOG_DIR) { $env:EVE_SENTRY_CHATLOG_DIR } else { "" }),
    [string]$ChannelState = $(if ($env:EVE_SENTRY_CHANNEL_STATE) { $env:EVE_SENTRY_CHANNEL_STATE } else { Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "EVE Sentry\channel_offsets.json" }),
    [string]$System = $(if ($env:EVE_SENTRY_SYSTEM) { $env:EVE_SENTRY_SYSTEM } else { "" }),
    [string]$OcrDevice = $(if ($env:EVE_SENTRY_OCR_DEVICE) { $env:EVE_SENTRY_OCR_DEVICE } else { "" }),
    [double]$HeartbeatInterval = 15.0,
    [double]$Timeout = $(if ($env:EVE_SENTRY_INTEL_TIMEOUT) { [double]$env:EVE_SENTRY_INTEL_TIMEOUT } else { 3.0 }),
    [string]$Python = "",
    [string]$LogDir = $(if ($env:EVE_SENTRY_MONITOR_LOG_DIR) { $env:EVE_SENTRY_MONITOR_LOG_DIR } else { Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "EVE Sentry\logs" }),
    [switch]$NoPublish,
    [switch]$NoEsiLocation,
    [switch]$AutoStart,
    [switch]$Background,
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
    EVE_SENTRY_INTEL_TIMEOUT = [string]$Timeout
}

if ($Channel) { $envUpdates["EVE_SENTRY_CHANNEL"] = $Channel }
if ($ChatlogDir) { $envUpdates["EVE_SENTRY_CHATLOG_DIR"] = $ChatlogDir }
if ($System) { $envUpdates["EVE_SENTRY_SYSTEM"] = $System }
if ($OcrDevice) { $envUpdates["EVE_SENTRY_OCR_DEVICE"] = $OcrDevice }
if ($NoPublish) { $envUpdates["EVE_SENTRY_PUBLISH_INTEL"] = "0" }
if ($NoEsiLocation) { $envUpdates["EVE_SENTRY_USE_ESI_LOCATION"] = "0" }
if ($AutoStart) { $envUpdates["EVE_SENTRY_AUTO_START_MONITOR"] = "1" }

$clientArgs = @("-m", "app.detector_client")

function ConvertTo-PowerShellLiteral {
    param([string]$Value)
    return "'" + ($Value -replace "'", "''") + "'"
}

function New-EncodedMonitorClientCommand {
    param(
        [string]$WorkingDirectory,
        [string]$PythonPath,
        [string[]]$Arguments,
        [System.Collections.IDictionary]$EnvironmentUpdates
    )

    $envLines = @()
    foreach ($key in $EnvironmentUpdates.Keys) {
        $envLines += "Set-Item -Path $(ConvertTo-PowerShellLiteral "Env:$key") -Value $(ConvertTo-PowerShellLiteral $EnvironmentUpdates[$key])"
    }
    $argumentList = ($Arguments | ForEach-Object { ConvertTo-PowerShellLiteral $_ }) -join ", "
    $script = @"
`$ErrorActionPreference = "Stop"
$($envLines -join "`n")
Set-Location -LiteralPath $(ConvertTo-PowerShellLiteral $WorkingDirectory)
& $(ConvertTo-PowerShellLiteral $PythonPath) @($argumentList)
exit `$LASTEXITCODE
"@
    return [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script))
}

function ConvertTo-WindowsArgument {
    param([string]$Value)

    if ($null -eq $Value) {
        return '""'
    }
    if ($Value.Length -eq 0) {
        return '""'
    }
    if ($Value -notmatch '[\s"]') {
        return $Value
    }

    $result = '"'
    $backslashes = 0
    foreach ($char in $Value.ToCharArray()) {
        if ($char -eq '\') {
            $backslashes += 1
            continue
        }
        if ($char -eq '"') {
            $result += ('\' * (($backslashes * 2) + 1)) + '"'
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            $result += '\' * $backslashes
            $backslashes = 0
        }
        $result += $char
    }
    if ($backslashes -gt 0) {
        $result += '\' * ($backslashes * 2)
    }
    $result += '"'
    return $result
}

function Normalize-ProcessPathEnvironment {
    $pathValue = [Environment]::GetEnvironmentVariable("Path", "Process")
    if (-not $pathValue) {
        $pathValue = [Environment]::GetEnvironmentVariable("PATH", "Process")
    }
    if (-not $pathValue) {
        return
    }

    [Environment]::SetEnvironmentVariable("PATH", $null, "Process")
    [Environment]::SetEnvironmentVariable("Path", $pathValue, "Process")
}

$stdout = Join-Path $LogDir "monitor-client.out.log"
$stderr = Join-Path $LogDir "monitor-client.err.log"
$encodedCommand = New-EncodedMonitorClientCommand `
    -WorkingDirectory $repoRoot `
    -PythonPath $Python `
    -Arguments $clientArgs `
    -EnvironmentUpdates $envUpdates

if ($PrintCommand) {
    [PSCustomObject]@{
        python = $Python
        cwd = $repoRoot
        args = $clientArgs
        env = $envUpdates
        background = [bool]$Background
        log_dir = $LogDir
        stdout = $stdout
        stderr = $stderr
        encoded_command = $encodedCommand
    } | ConvertTo-Json -Depth 4
    exit 0
}

if ($Background) {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    foreach ($key in $envUpdates.Keys) {
        Set-Item -Path "Env:$key" -Value $envUpdates[$key]
    }
    Normalize-ProcessPathEnvironment
    $argumentLine = ($clientArgs | ForEach-Object { ConvertTo-WindowsArgument $_ }) -join " "
    $process = Start-Process `
        -FilePath $Python `
        -ArgumentList $argumentLine `
        -WorkingDirectory $repoRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -PassThru
    [PSCustomObject]@{
        pid = $process.Id
        stdout = $stdout
        stderr = $stderr
        channel_state = $ChannelState
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
