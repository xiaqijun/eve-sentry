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


def test_release_workflow_uses_own_release_repository() -> None:
    workflow = (ROOT / ".github/workflows/release-client.yml").read_text(
        encoding="utf-8"
    )

    assert 'tags: ["v*"]' in workflow
    assert 'workflows: ["Client CI"]' in workflow
    assert "types: [completed]" in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
    assert "github.event.workflow_run.event == 'push'" in workflow
    assert "github.event.workflow_run.head_branch == 'main'" in workflow
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in workflow
    assert 'github.ref == \'refs/heads/main\'' in workflow
    assert "startsWith(github.ref, 'refs/tags/v')" in workflow
    assert "runs-on: ubuntu-latest" in workflow
    assert "environment: client-release" in workflow
    assert "RELEASE_REPOSITORY: xiaqijun/eve-sentry-client" in workflow
    assert '-Repository "xiaqijun/eve-sentry-client"' in workflow
    assert 'ref: ${{ github.event.workflow_run.head_sha || github.sha }}' in workflow
    assert '-ReleaseTarget "${{ needs.check-release.outputs.release_sha }}"' in workflow
    assert '-Version "${{ needs.check-release.outputs.version }}"' in workflow
    assert "if: needs.check-release.outputs.run_tests == 'true'" in workflow
    assert "secrets.EVE_SENTRY_RELEASE_TOKEN" not in workflow
    assert "GH_TOKEN: ${{ github.token }}" in workflow
    assert "$global:LASTEXITCODE = 0" in workflow
    assert "EVE_SENTRY_UPDATE_SIGNING_PRIVATE_KEY_B64 is required" in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "permissions:\n      contents: write" in workflow


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
