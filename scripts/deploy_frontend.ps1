<#
.SYNOPSIS
Build and deploy the React workbench to the production static directory.

.DESCRIPTION
Runs a deterministic frontend install, tests, and build; uploads a compressed
artifact over SSH; creates a timestamped remote backup; deploys with rsync; and
verifies both the backend health endpoint and the public static assets.

The remote script restores the backup automatically when extraction, sync, or
the remote health check fails after deployment starts.

.EXAMPLE
.\scripts\deploy_frontend.ps1 `
  -Target root@YOUR_SERVER `
  -IdentityFile "$HOME\.ssh\eve_server_key" `
  -PublicUrl http://YOUR_SERVER
#>

[CmdletBinding()]
param(
    [string]$Target = $env:EVE_SENTRY_DEPLOY_TARGET,
    [string]$IdentityFile = $env:EVE_SENTRY_DEPLOY_IDENTITY_FILE,
    [ValidatePattern('^/[A-Za-z0-9_./-]+$')]
    [string]$RemoteRoot = "/opt/1panel/www/eve-sentry",
    [ValidatePattern('^https?://[A-Za-z0-9_.:/-]+$')]
    [string]$RemoteHealthUrl = "http://127.0.0.1:8765/api/health",
    [ValidatePattern('^(|https?://[A-Za-z0-9_.:/-]+)$')]
    [string]$PublicUrl = "",
    [switch]$SkipInstall,
    [switch]$SkipTests
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-CheckedNative {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Description,
        [Parameter(Mandatory = $true)]
        [scriptblock]$Command
    )

    Write-Host "==> $Description"
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

if ([string]::IsNullOrWhiteSpace($Target)) {
    throw "Specify -Target or set EVE_SENTRY_DEPLOY_TARGET (for example, root@YOUR_SERVER)."
}
if ($Target -notmatch '^[A-Za-z0-9_.@-]+$') {
    throw "Target may only contain a user, host name, IPv4 address, dots, underscores, and hyphens."
}

$requiredCommands = @("npm", "tar", "ssh", "scp")
foreach ($commandName in $requiredCommands) {
    if (-not (Get-Command $commandName -ErrorAction SilentlyContinue)) {
        throw "Required command '$commandName' was not found in PATH."
    }
}

$sshOptions = @(
    "-o", "BatchMode=yes",
    "-o", "IdentitiesOnly=yes",
    "-o", "ConnectTimeout=15"
)
if (-not [string]::IsNullOrWhiteSpace($IdentityFile)) {
    $resolvedIdentity = (Resolve-Path -LiteralPath $IdentityFile).Path
    $sshOptions += @("-i", $resolvedIdentity)
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$frontendRoot = Join-Path $repoRoot "frontend"
$distRoot = Join-Path $frontendRoot "dist"
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$archiveName = "eve-sentry-frontend-$timestamp-$PID.tar.gz"
$archivePath = Join-Path ([System.IO.Path]::GetTempPath()) $archiveName
$remoteArchive = "/tmp/$archiveName"

try {
    Push-Location $frontendRoot
    try {
        if (-not $SkipInstall) {
            Invoke-CheckedNative "Installing locked frontend dependencies" { & npm ci }
        }
        if (-not $SkipTests) {
            Invoke-CheckedNative "Running frontend tests" { & npm test }
        }
        Invoke-CheckedNative "Building production frontend" { & npm run build }
    }
    finally {
        Pop-Location
    }

    $indexPath = Join-Path $distRoot "index.html"
    if (-not (Test-Path -LiteralPath $indexPath -PathType Leaf)) {
        throw "Build output is missing index.html."
    }

    $indexContent = Get-Content -LiteralPath $indexPath -Raw
    $assetMatch = [regex]::Match($indexContent, 'src="(?<path>/assets/index-[^"]+\.js)"')
    if (-not $assetMatch.Success) {
        throw "Could not find the production JavaScript entry in index.html."
    }
    $assetPath = $assetMatch.Groups["path"].Value
    $assetRelativePath = $assetPath.TrimStart("/").Replace("/", [System.IO.Path]::DirectorySeparatorChar)
    $localAssetPath = Join-Path $distRoot $assetRelativePath
    if (-not (Test-Path -LiteralPath $localAssetPath -PathType Leaf)) {
        throw "Build output is missing $assetPath."
    }

    $localIndexHash = (Get-FileHash -LiteralPath $indexPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $localAssetHash = (Get-FileHash -LiteralPath $localAssetPath -Algorithm SHA256).Hash.ToLowerInvariant()

    Invoke-CheckedNative "Packaging frontend artifact" {
        & tar -czf $archivePath -C $distRoot "."
    }

    $probeCommand = "set -eu; test -d '$RemoteRoot'; command -v bash >/dev/null; command -v rsync >/dev/null; command -v tar >/dev/null; command -v curl >/dev/null"
    Invoke-CheckedNative "Checking remote deployment prerequisites" {
        & ssh @sshOptions $Target $probeCommand
    }

    Invoke-CheckedNative "Uploading frontend artifact" {
        & scp @sshOptions $archivePath "${Target}:$remoteArchive"
    }

    $remoteScript = @'
set -euo pipefail

remote_root="$1"
health_url="$2"
archive="$3"
timestamp="$(date +%Y%m%d-%H%M%S)"
staging="$(mktemp -d /tmp/eve-sentry-frontend.XXXXXX)"
backup_root="${remote_root%/}-backups"
backup="$backup_root/$timestamp"
deployment_started=0
deployment_complete=0

finish() {
    status=$?
    trap - EXIT
    if [ "$status" -ne 0 ] && [ "$deployment_started" -eq 1 ] && [ "$deployment_complete" -eq 0 ]; then
        echo "Deployment failed; restoring $backup" >&2
        rsync -a --delete "$backup/" "$remote_root/" || true
    fi
    rm -rf "$staging" "$archive"
    exit "$status"
}
trap finish EXIT

mkdir -p "$backup_root" "$backup"
tar -xzf "$archive" -C "$staging"
test -s "$staging/index.html"

asset_path="$(sed -n 's/.*src="\([^"]*\/assets\/index-[^"]*\.js\)".*/\1/p' "$staging/index.html" | head -n 1)"
test -n "$asset_path"
test -s "$staging/${asset_path#/}"

cp -a "$remote_root/." "$backup/"
deployment_started=1
rsync -a --delete "$staging/" "$remote_root/"

test -s "$remote_root/index.html"
test -s "$remote_root/${asset_path#/}"
curl -fsS "$health_url" >/dev/null

index_sha256="$(sha256sum "$remote_root/index.html" | awk '{print $1}')"
asset_sha256="$(sha256sum "$remote_root/${asset_path#/}" | awk '{print $1}')"
deployment_complete=1

echo "BACKUP=$backup"
echo "INDEX_SHA256=$index_sha256"
echo "ASSET_SHA256=$asset_sha256"
'@

    $remoteCommand = "bash -s -- '$RemoteRoot' '$RemoteHealthUrl' '$remoteArchive'"
    Write-Host "==> Deploying with remote backup and rollback protection"
    $remoteOutput = $remoteScript | & ssh @sshOptions $Target $remoteCommand 2>&1
    if ($LASTEXITCODE -ne 0) {
        $remoteOutput | ForEach-Object { Write-Host $_ }
        throw "Remote deployment failed with exit code $LASTEXITCODE."
    }
    $remoteOutput | ForEach-Object { Write-Host $_ }

    $remoteIndexHash = ($remoteOutput | Where-Object { $_ -like "INDEX_SHA256=*" } | Select-Object -Last 1) -replace '^INDEX_SHA256=', ''
    $remoteAssetHash = ($remoteOutput | Where-Object { $_ -like "ASSET_SHA256=*" } | Select-Object -Last 1) -replace '^ASSET_SHA256=', ''
    if ($remoteIndexHash -ne $localIndexHash) {
        throw "Remote index.html hash does not match the local build."
    }
    if ($remoteAssetHash -ne $localAssetHash) {
        throw "Remote JavaScript hash does not match the local build."
    }

    if (-not [string]::IsNullOrWhiteSpace($PublicUrl)) {
        $publicBase = $PublicUrl.TrimEnd("/")
        Write-Host "==> Verifying public frontend and API"
        $publicIndex = Invoke-WebRequest -UseBasicParsing -Uri "$publicBase/?deploy=$timestamp" -Headers @{ "Cache-Control" = "no-cache" } -TimeoutSec 20
        if ($publicIndex.StatusCode -ne 200 -or $publicIndex.Content -notmatch [regex]::Escape($assetPath)) {
            throw "The public index does not reference the newly built JavaScript asset."
        }
        $publicAsset = Invoke-WebRequest -UseBasicParsing -Method Head -Uri "$publicBase$assetPath" -TimeoutSec 20
        if ($publicAsset.StatusCode -ne 200) {
            throw "The public JavaScript asset returned HTTP $($publicAsset.StatusCode)."
        }
        $publicHealth = Invoke-RestMethod -Uri "$publicBase/api/health" -TimeoutSec 20
        if (-not $publicHealth.health.ok) {
            throw "The public API health check did not report health.ok=true."
        }
    }

    Write-Host "Deployment completed successfully."
    Write-Host "Target: $Target"
    Write-Host "Remote root: $RemoteRoot"
    Write-Host "Asset: $assetPath"
    Write-Host "SHA256: $localAssetHash"
}
finally {
    if (Test-Path -LiteralPath $archivePath) {
        Remove-Item -LiteralPath $archivePath -Force
    }
}
