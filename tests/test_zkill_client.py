import gzip
import json
from unittest.mock import patch
from urllib.error import URLError

from app.esi.cache import EsiCache
from app.killboard.zkill_client import ZKillboardClient


class FakeResponse:
    def __init__(self, payload, headers=None):
        self.payload = payload
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self.payload


def test_zkill_client_reads_gzip_json_and_sets_headers(tmp_path):
    payload = gzip.compress(json.dumps([{"killmail_id": 1}]).encode("utf-8"))
    seen_headers = {}

    def fake_urlopen(request, timeout):
        assert timeout == 5
        seen_headers.update(dict(request.header_items()))
        return FakeResponse(payload, {"Content-Encoding": "gzip"})

    with patch("app.killboard.zkill_client.urlopen", fake_urlopen):
        client = ZKillboardClient(timeout=5, user_agent="eve-sentry-test")
        rows = client.character_recent(123)

    assert rows == [{"killmail_id": 1}]
    assert seen_headers["User-agent"] == "eve-sentry-test"
    assert seen_headers["Accept-encoding"] == "gzip"


def test_zkill_client_uses_cache(tmp_path):
    calls = 0

    def fake_urlopen(request, timeout):
        nonlocal calls
        calls += 1
        return FakeResponse(json.dumps([{"killmail_id": 1}]).encode("utf-8"))

    cache = EsiCache(tmp_path / "zkill_cache.json")
    with patch("app.killboard.zkill_client.urlopen", fake_urlopen):
        client = ZKillboardClient(cache=cache)
        assert client.character_recent(123) == [{"killmail_id": 1}]
        assert client.character_recent(123) == [{"killmail_id": 1}]

    assert calls == 1


def test_zkill_client_returns_stale_cache_when_request_fails(tmp_path):
    path = tmp_path / "zkill_cache.json"
    cache_key = "zkill:/characterID/123/"
    path.write_text(
        json.dumps(
            {
                cache_key: {
                    "value": [{"killmail_id": 99}],
                    "expires_at": 0,
                }
            }
        ),
        encoding="utf-8",
    )

    def fake_urlopen(request, timeout):
        _ = request, timeout
        raise URLError("offline")

    cache = EsiCache(path)
    with patch("app.killboard.zkill_client.urlopen", fake_urlopen):
        client = ZKillboardClient(cache=cache)
        rows = client.character_recent(123)

    assert rows == [{"killmail_id": 99}]
