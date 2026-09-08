from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_bot_deploy_requires_main_branch() -> None:
    workflow = _read(".github/workflows/deploy-bot.yml")

    assert "github.event_name != 'pull_request' && github.ref == 'refs/heads/main'" in workflow


def test_server_deploy_excludes_download_worker() -> None:
    workflow = _read(".github/workflows/deploy-server.yml")

    assert '"deploy/**"' not in workflow
    assert '"deploy/ci/**"' in workflow
    assert '"deploy/linux/**"' in workflow
    assert "cp -a deploy/ci deploy/linux deployment/backend/deploy/" in workflow
    assert "cp -a app scripts deploy deployment/backend/" not in workflow


def test_contract_workflow_covers_server_client_and_bot() -> None:
    workflow = _read(".github/workflows/ci-contracts.yml")

    assert "server-contract:" in workflow
    assert "client-server-contract:" in workflow
    assert "bot-contract:" in workflow
    assert "tests/test_intel_client.py" in workflow
    assert "tests/test_http_server.py" in workflow
    assert "tests/test_alerts.py" in workflow


def test_gateway_deploy_requires_complete_health_contract() -> None:
    script = _read("esi-gateway/deploy/ci/deploy_esi_gateway.sh")
    workflow = _read(".github/workflows/deploy-esi-gateway.yml")

    assert 'payload.get("cache_entries")' in script
    assert "type(entries) is int and entries >= 0" in script
    assert "EsiClient(timeout=10).get_system(30000142)" in workflow
    assert 'payload.get("name") != "Jita"' in workflow
