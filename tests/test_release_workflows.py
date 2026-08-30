from pathlib import Path


def test_client_ci_is_scoped_to_client_paths_and_windows() -> None:
    workflow = Path(".github/workflows/ci-client.yml").read_text(encoding="utf-8")

    assert "runs-on: windows-latest" in workflow
    assert 'branches: [main]' in workflow
    assert '"app/**"' in workflow
    assert '"packaging/**"' in workflow
    assert '"tests/**"' in workflow
    assert "--ignore=tests/test_intel_client.py" in workflow
    assert "permissions:\n  contents: read" in workflow


def test_release_workflow_uses_explicit_legacy_release_repository() -> None:
    workflow = Path(".github/workflows/release-client.yml").read_text(
        encoding="utf-8"
    )

    assert 'tags: ["v*"]' in workflow
    assert 'github.ref == \'refs/heads/main\'' in workflow
    assert "startsWith(github.ref, 'refs/tags/v')" in workflow
    assert "environment: client-release" in workflow
    assert "RELEASE_REPOSITORY: xiaqijun/eve-sentry" in workflow
    assert '-Repository "xiaqijun/eve-sentry"' in workflow
    assert '-ReleaseTarget "main"' in workflow
    assert "secrets.EVE_SENTRY_RELEASE_TOKEN" in workflow
    assert "github.token" not in workflow


def test_publish_script_refuses_to_overwrite_github_release() -> None:
    script = Path("scripts/publish_client_release.ps1").read_text(encoding="utf-8")

    assert '[string]$Repository = "xiaqijun/eve-sentry"' in script
    assert "gh release view $tag --repo $Repository" in script
    assert "gh release create $tag" in script
    assert "--repo $Repository" in script
    assert "--target $ReleaseTarget" in script
    assert "refusing to overwrite it" in script
    assert "--clobber" not in script
    assert "git rev-parse HEAD" not in script
