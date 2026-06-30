import base64
import json
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

import pytest

from app.esi.sso import (
    AuthorizationSession,
    EsiSsoError,
    EsiTokenStore,
    EveSsoClient,
    LocalCallbackServer,
    SsoMetadata,
    TokenSet,
    build_pkce_challenge,
)


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._body


def jwt(payload):
    def encode(value):
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return f"{encode({'alg': 'none'})}.{encode(payload)}.sig"


def test_pkce_challenge_matches_rfc7636_example():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"

    assert (
        build_pkce_challenge(verifier)
        == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )


def test_authorization_session_builds_url_and_parses_callback():
    client = EveSsoClient(
        client_id="client-id",
        redirect_uri="http://127.0.0.1:8765/callback",
        scopes=["esi-location.read_location.v1"],
        metadata=SsoMetadata(
            authorization_endpoint="https://login.test/authorize",
            token_endpoint="https://login.test/token",
        ),
    )

    session = client.create_authorization_session(state="fixed-state")
    parsed = urlparse(session.authorization_url)
    query = parse_qs(parsed.query)

    assert parsed.geturl().startswith("https://login.test/authorize?")
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["client-id"]
    assert query["redirect_uri"] == ["http://127.0.0.1:8765/callback"]
    assert query["scope"] == ["esi-location.read_location.v1"]
    assert query["state"] == ["fixed-state"]
    assert query["code_challenge_method"] == ["S256"]
    assert client.parse_callback_url(
        session,
        "http://127.0.0.1:8765/callback?code=abc&state=fixed-state",
    ) == "abc"

    with pytest.raises(EsiSsoError):
        client.parse_callback_url(
            session,
            "http://127.0.0.1:8765/callback?code=abc&state=wrong",
        )


def test_exchange_code_posts_pkce_payload_and_decodes_character_claims():
    requests = []
    token = jwt(
        {
            "sub": "CHARACTER:EVE:123",
            "owner": "owner-hash",
            "scp": ["esi-location.read_location.v1"],
        }
    )

    def opener(request, timeout):
        requests.append(request)
        return FakeResponse(
            {
                "access_token": token,
                "token_type": "Bearer",
                "expires_in": 1200,
                "refresh_token": "refresh-token",
            }
        )

    client = EveSsoClient(
        client_id="client-id",
        redirect_uri="http://127.0.0.1:8765/callback",
        metadata=SsoMetadata(token_endpoint="https://login.test/token"),
        opener=opener,
    )
    session = AuthorizationSession(
        authorization_url="https://login.test/authorize",
        state="fixed-state",
        redirect_uri="http://127.0.0.1:8765/callback",
        code_verifier="verifier",
        scopes=["esi-location.read_location.v1"],
    )

    tokens = client.exchange_code("auth-code", session)
    body = parse_qs(requests[0].data.decode("utf-8"))

    assert body["grant_type"] == ["authorization_code"]
    assert body["code"] == ["auth-code"]
    assert body["client_id"] == ["client-id"]
    assert body["redirect_uri"] == ["http://127.0.0.1:8765/callback"]
    assert body["code_verifier"] == ["verifier"]
    assert tokens.character_id == 123
    assert tokens.character_owner_hash == "owner-hash"
    assert tokens.scopes == ["esi-location.read_location.v1"]
    assert tokens.refresh_token == "refresh-token"


def test_token_store_persists_token_set(tmp_path):
    tokens = TokenSet.from_payload(
        {
            "access_token": jwt({"sub": "CHARACTER:EVE:123"}),
            "token_type": "Bearer",
            "expires_in": 1200,
            "refresh_token": "refresh-token",
            "scope": "esi-location.read_location.v1",
        },
        now=lambda: 1000.0,
    )
    store = EsiTokenStore(tmp_path / "tokens.json")

    store.save(tokens)
    loaded = store.load()

    assert loaded is not None
    assert loaded.character_id == 123
    assert loaded.refresh_token == "refresh-token"
    assert loaded.expires_at == 2200.0


def test_local_callback_server_captures_callback_url():
    server = LocalCallbackServer(port=0)
    server.start()
    try:
        callback_url = f"{server.url}?code=abc&state=fixed-state"
        with urlopen(callback_url, timeout=3) as response:
            assert response.status == 200
            assert "SSO complete" in response.read().decode("utf-8")

        assert server.wait_for_callback(timeout_seconds=1) == callback_url
    finally:
        server.stop()
