param(
    [string]$Repository = "xiaqijun/eve-sentry",
    [string]$Release = "",
    [string]$Output = ".\.runtime\onnx-models",
    [string]$DownloadBaseUrl = "https://evesentrydownload.kisectool.com/download"
)

$ErrorActionPreference = "Stop"
$modelNames = @("PP-OCRv6_medium_det", "PP-OCRv6_medium_rec")
$repoRoot = Split-Path -Parent $PSScriptRoot
$outputRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot $Output))
$temporaryBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$temporaryRoot = Join-Path $temporaryBase ("eve-sentry-models-" + [guid]::NewGuid())
$archivePath = Join-Path $temporaryRoot "client.zip"
$extractRoot = Join-Path $temporaryRoot "extracted"

try {
    New-Item -ItemType Directory -Force -Path $temporaryRoot, $extractRoot | Out-Null
    if (-not $Release) {
        $Release = (gh release view --repo $Repository --json tagName --jq .tagName).Trim()
    }
    if (-not $Release) {
        throw "No prior client release is available for model bootstrapping."
    }
    $releaseData = gh release view $Release --repo $Repository --json assets | ConvertFrom-Json
    $asset = $releaseData.assets |
        Where-Object { $_.name -match '^EVE-Sentry-Monitor-ONNX-[0-9].*\.zip$' } |
        Select-Object -First 1
    if ($null -eq $asset) {
        throw "Release $Release does not contain a client package."
    }

    $downloadUrl = "$($DownloadBaseUrl.TrimEnd('/'))/$($asset.name)"
    curl.exe -fL --retry 5 --retry-delay 3 --connect-timeout 30 `
        --output $archivePath $downloadUrl
    if ($LASTEXITCODE -ne 0) {
        throw "Could not download $($asset.name) from $downloadUrl."
    }
    Expand-Archive -LiteralPath $archivePath -DestinationPath $extractRoot -Force

    $modelsDirectory = Get-ChildItem -Path $extractRoot -Directory -Recurse |
        Where-Object {
            (Test-Path -LiteralPath (Join-Path $_.FullName "$($modelNames[0])\model.onnx")) -and
            (Test-Path -LiteralPath (Join-Path $_.FullName "$($modelNames[1])\model.onnx"))
        } |
        Select-Object -First 1
    if ($null -eq $modelsDirectory) {
        throw "The release package does not contain both ONNX models."
    }

    foreach ($modelName in $modelNames) {
        $source = Join-Path $modelsDirectory.FullName "$modelName\model.onnx"
        $destinationDirectory = Join-Path $outputRoot $modelName
        New-Item -ItemType Directory -Force -Path $destinationDirectory | Out-Null
        Copy-Item -LiteralPath $source -Destination (Join-Path $destinationDirectory "model.onnx") -Force
    }

    [PSCustomObject]@{
        release = $Release
        output = $outputRoot
        models = @(
            $modelNames | ForEach-Object {
                $path = Join-Path $outputRoot "$_\model.onnx"
                [PSCustomObject]@{
                    name = $_
                    bytes = (Get-Item -LiteralPath $path).Length
                    sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
                }
            }
        )
    } | ConvertTo-Json -Depth 4
} finally {
    $resolvedTemporary = [IO.Path]::GetFullPath($temporaryRoot)
    if (
        $resolvedTemporary.StartsWith($temporaryBase, [StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path -Leaf $resolvedTemporary).StartsWith("eve-sentry-models-")
    ) {
        Remove-Item -LiteralPath $resolvedTemporary -Recurse -Force -ErrorAction SilentlyContinue
    }
}
