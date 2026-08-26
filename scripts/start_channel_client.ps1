param(
    [string]$Python = "",
    [switch]$PrintCommand
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $Python) {
    $venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
    $Python = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { "python" }
}

$arguments = @("-m", "app.channel_client_gui")
if ($PrintCommand) {
    [PSCustomObject]@{ python = $Python; cwd = $repoRoot; args = $arguments } |
        ConvertTo-Json -Depth 3
    exit 0
}

Push-Location $repoRoot
try {
    & $Python @arguments
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
