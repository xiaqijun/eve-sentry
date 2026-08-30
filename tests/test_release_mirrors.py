import json
from pathlib import Path
import shutil
import subprocess

import pytest


def test_client_release_manifest_contains_gitcode_mirrors():
    script = Path("scripts/publish_client_release.ps1").read_text(encoding="utf-8")

    assert '[string]$GitCodeRepository = ""' in script
    assert "https://gitcode.com/$($GitCodeRepository.Trim('/'))" in script
    assert "mirrors = $programMirrors" in script
    assert "mirrors = $modelMirrors" in script
    assert 'release_manifest.ps1' in script
    assert "ConvertTo-ReleaseManifestJson -Manifest $manifest" in script


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell is unavailable")
def test_release_manifest_serializer_preserves_nested_mirror_arrays():
    helper = Path("scripts/release_manifest.ps1").resolve()
    escaped_helper = str(helper).replace("'", "''")
    command = f"""
. '{escaped_helper}'
$manifest = [ordered]@{{
    version = '1.2.3'
    url = 'https://download.example/program.zip'
    mirrors = @('https://mirror.example/program.zip')
    components = [ordered]@{{
        models = [ordered]@{{
            url = 'https://download.example/models.zip'
            mirrors = @('https://mirror.example/models.zip')
        }}
    }}
}}
ConvertTo-ReleaseManifestJson -Manifest $manifest
"""
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    payload = json.loads(completed.stdout)

    assert payload["mirrors"] == ["https://mirror.example/program.zip"]
    assert payload["components"]["models"]["mirrors"] == [
        "https://mirror.example/models.zip"
    ]


def test_gitcode_release_script_keeps_token_out_of_public_urls():
    script = Path("scripts/publish_gitcode_release.ps1").read_text(encoding="utf-8")

    assert '[string]$Token = $env:GITCODE_TOKEN' in script
    assert '"PRIVATE-TOKEN" = $Token' in script
    assert "access_token=" not in script
    assert "releases/download/$tag" not in script
    assert "browser_download_url" in script
    assert '$TargetCommit = "main"' in script
    assert '[switch]$IncludeFullPackage' in script
    assert 'if ($IncludeFullPackage)' in script
    assert 'GitCode upload failed for $($asset.Name) (HTTP $status)' in script


def test_main_repository_has_retired_client_release_workflow():
    assert not Path(".github/workflows/release-client.yml").exists()
