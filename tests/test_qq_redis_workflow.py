"""Keep production ACL repairs behind the same protected deployment boundary."""

from pathlib import Path


def test_repair_requires_main_dispatch_production_and_strict_host_verification():
    workflow = Path(".github/workflows/repair-qq-redis.yml").read_text()
    assert "github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'" in workflow
    assert "environment: production" in workflow
    assert "needs: validate" in workflow
    assert 'EVE_SENTRY_REDIS_INTEGRATION: "1"' in workflow
    assert "StrictHostKeyChecking=yes" in workflow
    assert "EVE_SENTRY_DEPLOY_KNOWN_HOSTS" in workflow


def test_server_deploy_checks_bot_redis_before_restart():
    script = Path("deploy/ci/deploy_release.sh").read_text()
    check = script.index('"$backend_root/deploy/ci/check_qq_redis.py"')
    restart = script.index('systemctl restart "$service_name"', check)
    assert check < restart
