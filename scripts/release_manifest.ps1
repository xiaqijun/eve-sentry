function ConvertTo-ReleaseManifestJson {
    param(
        [Parameter(Mandatory = $true)]
        [System.Collections.IDictionary]$Manifest
    )

    $json = $Manifest | ConvertTo-Json -Depth 8
    $payload = $json | ConvertFrom-Json
    if ($payload.mirrors -isnot [System.Array]) {
        throw "Release manifest program mirrors must be a JSON array"
    }
    if ($payload.components.models.mirrors -isnot [System.Array]) {
        throw "Release manifest model mirrors must be a JSON array"
    }
    return $json
}
