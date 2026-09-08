from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_download_site_is_built_and_deployed_from_monorepo() -> None:
    ci = (ROOT / ".github/workflows/ci-download-site.yml").read_text(encoding="utf-8")
    deploy = (ROOT / ".github/workflows/deploy-download-site.yml").read_text(
        encoding="utf-8"
    )
    wrangler = (ROOT / "deploy/cloudflare-download/wrangler.jsonc").read_text(
        encoding="utf-8"
    )

    for workflow in (ci, deploy):
        assert '"download-site/**"' in workflow
        assert '"deploy/cloudflare-download/**"' in workflow
        assert '"GITHUB_REPO"[[:space:]]*:[[:space:]]*"eve-sentry"' in workflow
    assert '"GITHUB_REPO": "eve-sentry"' in wrangler
    assert "CLOUDFLARE_ACCOUNT_ID" in deploy
    assert "CLOUDFLARE_API_TOKEN" in deploy


def test_download_site_uses_canonical_public_documents() -> None:
    ci = (ROOT / ".github/workflows/ci-download-site.yml").read_text(encoding="utf-8")
    deploy = (ROOT / ".github/workflows/deploy-download-site.yml").read_text(
        encoding="utf-8"
    )
    page = (ROOT / "download-site/app/docs/client/page.tsx").read_text(
        encoding="utf-8"
    )

    assert "export default function ClientDocsPage()" in page
    assert 'readRepositoryDocument("client/docs/client.md")' in page
    assert (ROOT / "client/docs/client.md").is_file()
    assert not (ROOT / "docs/client.md").exists()
    for workflow in (ci, deploy):
        assert '"client/docs/client.md"' in workflow
        assert '"docs/server-deployment.md"' in workflow
