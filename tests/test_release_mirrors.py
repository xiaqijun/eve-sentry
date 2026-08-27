from pathlib import Path


def test_client_release_manifest_contains_gitcode_mirrors():
    script = Path("scripts/publish_client_release.ps1").read_text(encoding="utf-8")

    assert '[string]$GitCodeRepository = ""' in script
    assert "https://gitcode.com/$($GitCodeRepository.Trim('/'))" in script
    assert "mirrors = $programMirrors" in script
    assert "mirrors = $modelMirrors" in script


def test_gitcode_release_script_keeps_token_out_of_public_urls():
    script = Path("scripts/publish_gitcode_release.ps1").read_text(encoding="utf-8")

    assert '[string]$Token = $env:GITCODE_TOKEN' in script
    assert '"PRIVATE-TOKEN" = $Token' in script
    assert "access_token=" not in script
    assert "releases/download/$tag" not in script
    assert "browser_download_url" in script
    assert '$TargetCommit = "main"' in script


def test_release_workflow_publishes_and_verifies_gitcode_mirror():
    workflow = Path(".github/workflows/release-client.yml").read_text(
        encoding="utf-8"
    )

    assert '-GitCodeRepository "xiaqiqi/eve-sentry"' in workflow
    assert ".\\scripts\\publish_gitcode_release.ps1 -Version $version" in workflow
    assert "GITCODE_TOKEN: ${{ secrets.GITCODE_TOKEN }}" in workflow
