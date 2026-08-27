param(
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [string]$Owner = "xiaqiqi",
    [string]$Repository = "eve-sentry-releases",
    [string]$Token = $env:GITCODE_TOKEN,
    [string[]]$Assets = @(),
    [string]$TargetCommit = "",
    [switch]$IncludeFullPackage
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot

if ($Version -notmatch '^\d+\.\d+\.\d+([+-][0-9A-Za-z.-]+)?$') {
    throw "Invalid release version: $Version"
}
if (-not $Token) {
    throw "GITCODE_TOKEN is required"
}
if (-not $Owner -or -not $Repository) {
    throw "GitCode owner and repository are required"
}

$tag = "v$Version"
$apiBase = "https://api.gitcode.com/api/v5/repos/$Owner/$Repository"
$apiHeaders = @{ "PRIVATE-TOKEN" = $Token }

function Get-HttpStatusCode([System.Management.Automation.ErrorRecord]$ErrorRecord) {
    try {
        return [int]$ErrorRecord.Exception.Response.StatusCode
    } catch {
        return 0
    }
}

function Invoke-GitCodeGet([string]$Path, [switch]$AllowMissing) {
    try {
        return Invoke-RestMethod `
            -Headers $apiHeaders `
            -Uri "$apiBase$Path" `
            -Method Get `
            -TimeoutSec 60
    } catch {
        $status = Get-HttpStatusCode $_
        if ($AllowMissing -and $status -in @(400, 404)) {
            return $null
        }
        throw "GitCode API GET failed (HTTP $status)"
    }
}

if (-not $TargetCommit) {
    # GitCode source mirrors can lag behind GitHub pushes. Release binaries are
    # verified independently, so use the branch GitCode already has.
    $TargetCommit = "main"
}

if ($Assets.Count -eq 0) {
    $dist = Join-Path $repoRoot "dist"
    $manifestPath = Join-Path $dist "latest.json"
    if (-not (Test-Path -LiteralPath $manifestPath)) {
        throw "Release manifest is missing"
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    $modelAssetName = [string]$manifest.components.models.filename
    if (-not $modelAssetName -or [IO.Path]::GetFileName($modelAssetName) -ne $modelAssetName) {
        throw "Release manifest model filename is invalid"
    }
    $Assets = @(
        Join-Path $dist "EVE-Sentry-Monitor-ONNX-program-$Version.zip"
        Join-Path $dist "EVE-Sentry-Channel-$Version.zip"
        Join-Path $dist $modelAssetName
        $manifestPath
    )
    if ($IncludeFullPackage) {
        $Assets = @(
            Join-Path $dist "EVE-Sentry-Monitor-ONNX-$Version.zip"
            $Assets
        )
    }
}

$assetFiles = @(
    $Assets |
        Where-Object { $_ } |
        ForEach-Object { Get-Item -LiteralPath $_ }
)
if ($assetFiles.Count -lt 1) {
    throw "No GitCode release assets were found"
}

$release = Invoke-GitCodeGet "/releases/$tag" -AllowMissing
if ($null -eq $release) {
    $releaseBody = @{
        tag_name = $tag
        target_commitish = $TargetCommit
        name = "EVE Sentry v$Version"
        body = "EVE Sentry client release mirror."
    } | ConvertTo-Json
    try {
        $release = Invoke-RestMethod `
            -Headers ($apiHeaders + @{ "Content-Type" = "application/json" }) `
            -Uri "$apiBase/releases" `
            -Method Post `
            -Body ([Text.Encoding]::UTF8.GetBytes($releaseBody)) `
            -TimeoutSec 60
    } catch {
        $status = Get-HttpStatusCode $_
        throw "GitCode release creation failed (HTTP $status)"
    }
}

$existingNames = @(
    $release.assets |
        Where-Object { $_.type -eq "attach" } |
        ForEach-Object { [string]$_.name }
)
$duplicateNames = @(
    $assetFiles |
        Where-Object { $_.Name -in $existingNames } |
        ForEach-Object { $_.Name }
)
if ($duplicateNames.Count -gt 0) {
    throw "GitCode release attachments are immutable; publish a new version: $($duplicateNames -join ', ')"
}

foreach ($asset in $assetFiles) {
    $encodedName = [uri]::EscapeDataString($asset.Name)
    $uploaded = $false
    $lastUploadError = ""
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        $upload = Invoke-GitCodeGet "/releases/$tag/upload_url?file_name=$encodedName"
        if (-not $upload.url -or -not $upload.headers) {
            throw "GitCode did not return an upload target for $($asset.Name)"
        }
        $uploadHeaders = @{}
        foreach ($property in $upload.headers.psobject.Properties) {
            $uploadHeaders[$property.Name] = [string]$property.Value
        }
        try {
            $response = Invoke-WebRequest `
                -Uri $upload.url `
                -Method Put `
                -Headers $uploadHeaders `
                -InFile $asset.FullName `
                -TimeoutSec 600
            if ([int]$response.StatusCode -lt 200 -or [int]$response.StatusCode -ge 300) {
                throw "unexpected upload response"
            }
            $uploaded = $true
            break
        } catch {
            $status = Get-HttpStatusCode $_
            $lastUploadError = "HTTP ${status}: $($_.Exception.Message)"
            if ($attempt -lt 3) {
                Start-Sleep -Seconds (10 * $attempt)
            }
        }
    }
    if (-not $uploaded) {
        throw "GitCode upload failed for $($asset.Name) after 3 attempts: $lastUploadError"
    }
}

$release = Invoke-GitCodeGet "/releases/$tag"
$attachments = @($release.assets | Where-Object { $_.type -eq "attach" })
$published = @()
foreach ($asset in $assetFiles) {
    $attachment = $attachments | Where-Object { $_.name -eq $asset.Name } | Select-Object -First 1
    if ($null -eq $attachment -or -not $attachment.browser_download_url) {
        throw "GitCode release attachment is missing: $($asset.Name)"
    }
    $rangeEnd = [Math]::Min(1023, [Math]::Max(0, $asset.Length - 1))
    $rangeVerified = $false
    for ($attempt = 1; $attempt -le 12; $attempt++) {
        $headerOutput = & curl.exe -fsS -L -D - -o NUL `
            -H "Range: bytes=0-$rangeEnd" `
            $attachment.browser_download_url 2>$null
        if (
            $LASTEXITCODE -eq 0 -and
            $headerOutput -match "HTTP/\S+ 206" -and
            $headerOutput -match "Content-Range:\s*bytes 0-$rangeEnd/$($asset.Length)"
        ) {
            $rangeVerified = $true
            break
        }
        Start-Sleep -Seconds 5
    }
    if (-not $rangeVerified) {
        throw "GitCode range verification failed for $($asset.Name)"
    }
    $published += [pscustomobject]@{
        name = $asset.Name
        size = $asset.Length
        url = $attachment.browser_download_url
    }
}

[pscustomobject]@{
    tag = $tag
    repository = "$Owner/$Repository"
    assets = $published
} | ConvertTo-Json -Depth 5
