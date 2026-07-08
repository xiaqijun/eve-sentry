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
    [string]$LogDir = $(if ($env:EVE_SENTRY_ALERT_LOG_DIR) { $env:EVE_SENTRY_ALERT_LOG_DIR } else { Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "EVE Sentry\logs" }),
    [switch]$NoPopup,
    [switch]$NoDetails,
    [switch]$Ack,
    [switch]$Poll,
    [switch]$Once,
    [switch]$Json,
    [switch]$IncludeExisting,
    [switch]$UnacknowledgedOnly,
    [switch]$NoState,
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

function ConvertTo-PowerShellLiteral {
    param([string]$Value)
    return "'" + ($Value -replace "'", "''") + "'"
}

function New-EncodedAlertClientCommand {
    param(
        [string]$WorkingDirectory,
        [string]$PythonPath,
        [string[]]$Arguments
    )

    $argumentList = ($Arguments | ForEach-Object { ConvertTo-PowerShellLiteral $_ }) -join ", "
    $script = @"
`$ErrorActionPreference = "Stop"
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

$stdout = Join-Path $LogDir "alert-client.out.log"
$stderr = Join-Path $LogDir "alert-client.err.log"
$encodedCommand = New-EncodedAlertClientCommand -WorkingDirectory $repoRoot -PythonPath $Python -Arguments $clientArgs

if ($PrintCommand) {
    [PSCustomObject]@{
        python = $Python
        cwd = $repoRoot
        args = $clientArgs
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
        state = $State
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
