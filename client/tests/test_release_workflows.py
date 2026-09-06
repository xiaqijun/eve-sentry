from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_client_ci_is_scoped_to_client_paths_and_windows() -> None:
    workflow = (ROOT / ".github/workflows/ci-client.yml").read_text(encoding="utf-8")

    assert "runs-on: windows-latest" in workflow
    assert 'branches: [main]' in workflow
    assert '"client/**"' in workflow
    assert "working-directory: client" in workflow
    assert "--ignore=tests/test_intel_client.py" in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "cancel-in-progress: true" in workflow


def test_client_release_workflow_is_retired() -> None:
    assert not (ROOT / ".github/workflows/release-client.yml").exists()


def test_publish_script_refuses_to_overwrite_github_release() -> None:
    script = (ROOT / "client/scripts/publish_client_release.ps1").read_text(
        encoding="utf-8"
    )

    assert '[string]$Repository = "xiaqijun/eve-sentry-client"' in script
    assert (
        "https://github.com/xiaqijun/eve-sentry-client/releases/latest/download"
        in script
    )
    assert "gh release view $tag --repo $Repository" in script
    assert "gh release create $tag" in script
    assert "--repo $Repository" in script
    assert "--target $ReleaseTarget" in script
    assert "GitHub release target must be a full commit SHA" in script
    assert "eve-sentry-client-source.json" in script
    assert "refusing to overwrite it" in script
    assert "--clobber" not in script
    assert "$ReleaseTarget = (git rev-parse HEAD).Trim()" in script


def test_model_restore_defaults_to_client_release_repository() -> None:
    script = (ROOT / "client/scripts/restore_release_models.ps1").read_text(
        encoding="utf-8"
    )

    assert '[string]$Repository = "xiaqijun/eve-sentry-client"' in script
    assert "gh release view --repo $Repository" in script
    assert "gh release view $Release --repo $Repository" in script
    assert "gh release download $Release --repo $Repository" in script
