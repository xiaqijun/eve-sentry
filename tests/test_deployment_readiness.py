"""Regression tests for deployment readiness checks."""

from pathlib import Path


def test_release_deployment_defaults_to_private_readiness_probe() -> None:
    script = Path("deploy/ci/deploy_release.sh").read_text(encoding="utf-8")

    assert "http://127.0.0.1:8765/api/readyz" in script
    assert "Readiness check failed after deployment." in script


def test_github_deployment_verifies_public_readiness() -> None:
    workflow = Path(".github/workflows/deploy-server.yml").read_text(
        encoding="utf-8"
    )

    assert "Verify public readiness" in workflow
    assert '"${PUBLIC_URL%/}/api/readyz" > readiness.json' in workflow
    assert 'payload.get("ok")' in workflow


def test_frontend_deployment_uses_readiness_contract() -> None:
    script = Path("scripts/deploy_frontend.ps1").read_text(encoding="utf-8")

    assert (
        '[string]$RemoteHealthUrl = "http://127.0.0.1:8765/api/readyz"'
        in script
    )
    assert '$publicBase/api/readyz' in script
    assert "$publicReadiness.ok" in script
    assert "$publicHealth.health.ok" not in script
