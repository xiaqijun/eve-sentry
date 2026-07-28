param(
    [string]$Version = "",
    [string]$Python = ".\.venv\Scripts\python.exe",
    [string]$DownloadBaseUrl = "https://github.com/xiaqijun/eve-sentry/releases/latest/download",
    [switch]$SkipBuild,
    [switch]$SkipGithub
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    if (-not $Version) {
        $Version = (& $Python -c "from app.version import APP_VERSION; print(APP_VERSION)").Trim()
    }
    if ($Version -notmatch '^\d+\.\d+\.\d+([+-][0-9A-Za-z.-]+)?$') {
        throw "Invalid release version: $Version"
    }

    $buildName = "EVE-Sentry-Monitor-ONNX"
    $assetName = "$buildName-$Version.zip"
    $assetPath = Join-Path $repoRoot "dist\$assetName"
    $manifestPath = Join-Path $repoRoot "dist\latest.json"

    if (-not $SkipBuild) {
        $env:EVE_SENTRY_ONNX_MODEL_CACHE = Join-Path $repoRoot ".runtime\onnx-models"
        & $Python -m PyInstaller --clean --noconfirm packaging\eve-sentry-monitor-onnx.spec
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
        Compress-Archive -Path ".\dist\$buildName" -DestinationPath $assetPath -Force
    }
    if (-not (Test-Path -LiteralPath $assetPath)) {
        throw "Release package is missing: $assetPath"
    }

    $file = Get-Item -LiteralPath $assetPath
    $hash = (Get-FileHash -LiteralPath $assetPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $downloadUrl = "$($DownloadBaseUrl.TrimEnd('/'))/$assetName"
    $manifest = [ordered]@{
        version = $Version
        url = $downloadUrl
        sha256 = $hash
        size = $file.Length
        filename = $assetName
        released_at = [DateTimeOffset]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
    }
    $manifestJson = $manifest | ConvertTo-Json
    [IO.File]::WriteAllText(
        $manifestPath,
        $manifestJson,
        [Text.UTF8Encoding]::new($false)
    )

    if (-not $SkipGithub) {
        $tag = "v$Version"
        $previousErrorAction = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        gh release view $tag --json tagName 2>$null | Out-Null
        $releaseExists = $LASTEXITCODE -eq 0
        $ErrorActionPreference = $previousErrorAction
        if ($releaseExists) {
            gh release upload $tag $assetPath $manifestPath --clobber
        } else {
            gh release create $tag $assetPath $manifestPath --target HEAD --title "EVE Sentry v$Version" --generate-notes
        }
        if ($LASTEXITCODE -ne 0) { throw "GitHub release failed with exit code $LASTEXITCODE" }
    }

    [PSCustomObject]@{
        version = $Version
        package = $assetPath
        sha256 = $hash
        size = $file.Length
        manifest = $manifestPath
        download_url = $downloadUrl
    } | ConvertTo-Json
} finally {
    Pop-Location
}
