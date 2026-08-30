param(
    [string]$Version = "",
    [string]$Python = ".\.venv\Scripts\python.exe",
    [string]$DownloadBaseUrl = "https://github.com/xiaqijun/eve-sentry-client/releases/latest/download",
    [string]$Repository = "xiaqijun/eve-sentry-client",
    [string]$ReleaseTarget = "",
    [string]$GitCodeRepository = "",
    [switch]$SkipBuild,
    [switch]$SkipGithub
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "release_manifest.ps1")
Push-Location $repoRoot
try {
    if (-not $Version) {
        $Version = (& $Python -c "from app.version import APP_VERSION; print(APP_VERSION)").Trim()
    }
    if ($Version -notmatch '^\d+\.\d+\.\d+([+-][0-9A-Za-z.-]+)?$') {
        throw "Invalid release version: $Version"
    }
    if ($Repository -notmatch '^[^/\s]+/[^/\s]+$') {
        throw "Invalid GitHub release repository: $Repository"
    }
    if (-not $ReleaseTarget.Trim()) {
        $ReleaseTarget = (git rev-parse HEAD).Trim()
    }
    if ($ReleaseTarget -notmatch '^[0-9a-fA-F]{40}$') {
        throw "GitHub release target must be a full commit SHA: $ReleaseTarget"
    }

    $buildName = "EVE-Sentry-Monitor-ONNX"
    $assetName = "$buildName-$Version.zip"
    $assetPath = Join-Path $repoRoot "dist\$assetName"
    $channelBuildName = "EVE-Sentry-Channel"
    $channelAssetName = "$channelBuildName-$Version.zip"
    $channelAssetPath = Join-Path $repoRoot "dist\$channelAssetName"
    $programAssetName = "$buildName-program-$Version.zip"
    $programAssetPath = Join-Path $repoRoot "dist\$programAssetName"
    $manifestPath = Join-Path $repoRoot "dist\latest.json"
    $sourceMetadataPath = Join-Path $repoRoot "dist\eve-sentry-client-source.json"

    & $Python scripts\update_signing.py prepare-public resources\update_public_key.pem
    if ($LASTEXITCODE -ne 0) { throw "Preparing update signing key failed" }

    if (-not $SkipBuild) {
        $env:EVE_SENTRY_ONNX_MODEL_CACHE = Join-Path $repoRoot ".runtime\onnx-models"
        & $Python -m PyInstaller --clean --noconfirm packaging\eve-sentry-monitor-onnx.spec
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
        $builtRoot = Join-Path $repoRoot "dist\$buildName"
        $builtModelDir = Join-Path $builtRoot "_internal\models"
        if (-not (Test-Path -LiteralPath $builtModelDir)) {
            $builtModelDir = Join-Path $builtRoot "models"
        }
        if (-not (Test-Path -LiteralPath $builtModelDir)) {
            throw "Built OCR model directory is missing"
        }
        $modelHashes = @(
            Get-ChildItem -LiteralPath $builtModelDir -Recurse -Filter model.onnx |
                Sort-Object FullName |
                ForEach-Object { (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant() }
        )
        $modelVersionBytes = [Text.Encoding]::UTF8.GetBytes(($modelHashes -join ''))
        $modelVersion = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($modelVersionBytes)).ToLowerInvariant()
        [IO.File]::WriteAllText(
            (Join-Path $builtModelDir "version.json"),
            (@{ version = $modelVersion } | ConvertTo-Json),
            [Text.UTF8Encoding]::new($false)
        )
        Compress-Archive -Path ".\dist\$buildName" -DestinationPath $assetPath -Force

        & $Python -m PyInstaller --clean --noconfirm packaging\eve-sentry-channel.spec
        if ($LASTEXITCODE -ne 0) { throw "Channel client PyInstaller failed with exit code $LASTEXITCODE" }
        $channelBuiltRoot = Join-Path $repoRoot "dist\$channelBuildName"
        if (-not (Test-Path -LiteralPath (Join-Path $channelBuiltRoot "$channelBuildName.exe"))) {
            throw "Built channel client executable is missing"
        }
        Compress-Archive -Path ".\dist\$channelBuildName" -DestinationPath $channelAssetPath -Force

        $programStage = Join-Path $repoRoot "dist\.program-stage-$Version"
        $modelStage = Join-Path $repoRoot "dist\.model-stage-$Version"
        Remove-Item -LiteralPath $programStage,$modelStage -Recurse -Force -ErrorAction SilentlyContinue
        $programRoot = Join-Path $programStage $buildName
        New-Item -ItemType Directory -Path $programRoot -Force | Out-Null
        $null = & robocopy $builtRoot $programRoot /E /R:2 /W:1 /XD $builtModelDir
        if ($LASTEXITCODE -ge 8) { throw "Program component staging failed with exit code $LASTEXITCODE" }
        Compress-Archive -Path $programRoot -DestinationPath $programAssetPath -Force
        $modelRoot = Join-Path $modelStage "models"
        New-Item -ItemType Directory -Path $modelRoot -Force | Out-Null
        $null = & robocopy $builtModelDir $modelRoot /MIR /R:2 /W:1
        if ($LASTEXITCODE -ge 8) { throw "Model component staging failed with exit code $LASTEXITCODE" }
        $modelAssetName = "$buildName-models-$modelVersion.zip"
        $modelAssetPath = Join-Path $repoRoot "dist\$modelAssetName"
        Compress-Archive -Path $modelRoot -DestinationPath $modelAssetPath -Force
        Remove-Item -LiteralPath $programStage,$modelStage -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($SkipBuild) {
        $modelCandidate = Get-ChildItem -LiteralPath (Join-Path $repoRoot "dist") -Filter "$buildName-models-*.zip" |
            Sort-Object LastWriteTimeUtc -Descending |
            Select-Object -First 1
        if ($null -ne $modelCandidate) {
            $modelAssetPath = $modelCandidate.FullName
            $modelAssetName = $modelCandidate.Name
            $modelVersion = $modelCandidate.BaseName.Substring("$buildName-models-".Length)
        }
    }
    if (-not (Test-Path -LiteralPath $assetPath) -or -not (Test-Path -LiteralPath $programAssetPath) -or -not (Test-Path -LiteralPath $channelAssetPath)) {
        throw "Release client package is missing"
    }

    $file = Get-Item -LiteralPath $programAssetPath
    $hash = (Get-FileHash -LiteralPath $programAssetPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $downloadUrl = "$($DownloadBaseUrl.TrimEnd('/'))/${programAssetName}?sha256=${hash}&release=$([uri]::EscapeDataString($Version))"
    $programMirrors = @()
    if ($GitCodeRepository) {
        $gitCodeBaseUrl = "https://gitcode.com/$($GitCodeRepository.Trim('/'))/releases/download/v$Version"
        $programMirrors += "$gitCodeBaseUrl/$programAssetName"
    }
    if (-not $modelAssetPath -or -not (Test-Path -LiteralPath $modelAssetPath)) {
        throw "Release model package is missing"
    }
    $modelFile = Get-Item -LiteralPath $modelAssetPath
    $modelHash = (Get-FileHash -LiteralPath $modelAssetPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $modelDownloadUrl = "$($DownloadBaseUrl.TrimEnd('/'))/${modelAssetName}?sha256=${modelHash}&release=$([uri]::EscapeDataString($Version))"
    $modelMirrors = @()
    if ($GitCodeRepository) {
        $modelMirrors += "$gitCodeBaseUrl/$modelAssetName"
    }
    $manifest = [ordered]@{
        version = $Version
        url = $downloadUrl
        mirrors = $programMirrors
        sha256 = $hash
        size = $file.Length
        filename = $programAssetName
        released_at = [DateTimeOffset]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
        components = [ordered]@{
            models = [ordered]@{
                version = $modelVersion
                url = $modelDownloadUrl
                mirrors = $modelMirrors
                sha256 = $modelHash
                size = $modelFile.Length
                filename = $modelAssetName
            }
        }
    }
    $manifestJson = ConvertTo-ReleaseManifestJson -Manifest $manifest
    [IO.File]::WriteAllText(
        $manifestPath,
        $manifestJson,
        [Text.UTF8Encoding]::new($false)
    )
    & $Python scripts\update_signing.py sign $manifestPath
    if ($LASTEXITCODE -ne 0) { throw "Signing update manifest failed" }

    $workflowCommit = (git rev-parse HEAD).Trim()
    $sourceMetadata = [ordered]@{
        source_repository = $Repository
        source_commit = $ReleaseTarget.ToLowerInvariant()
        release_repository = $Repository
        release_workflow_commit = $workflowCommit
        version = $Version
    }
    [IO.File]::WriteAllText(
        $sourceMetadataPath,
        ($sourceMetadata | ConvertTo-Json),
        [Text.UTF8Encoding]::new($false)
    )

    if (-not $SkipGithub) {
        $tag = "v$Version"
        $previousErrorAction = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        gh release view $tag --repo $Repository --json tagName 2>$null | Out-Null
        $releaseExists = $LASTEXITCODE -eq 0
        $ErrorActionPreference = $previousErrorAction
        if ($releaseExists) {
            throw "GitHub release $Repository@$tag already exists; refusing to overwrite it"
        }
        gh release create $tag $assetPath $programAssetPath $channelAssetPath $modelAssetPath $manifestPath $sourceMetadataPath `
            --repo $Repository `
            --target $ReleaseTarget `
            --title "EVE Sentry v$Version" `
            --generate-notes
        if ($LASTEXITCODE -ne 0) { throw "GitHub release failed with exit code $LASTEXITCODE" }
    }

    [PSCustomObject]@{
        version = $Version
        package = $programAssetPath
        sha256 = $hash
        size = $file.Length
        manifest = $manifestPath
        source_metadata = $sourceMetadataPath
        download_url = $downloadUrl
        channel_package = $channelAssetPath
    } | ConvertTo-Json
} finally {
    Pop-Location
}
