"""Thread-local freshness and throttle compatibility across public ESI requests."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from app.esi.client import EsiApiError
from app.esi.remote import RemoteEsiClient
from tests.test_esi_remote import FakeResponse


def test_remote_freshness_is_per_thread_and_old_gateway_is_unknown():
    barrier = threading.Barrier(2)
    def opener(request, timeout):
        cid = json.loads(request.data)[0]
        return FakeResponse({"data": [{"character_id": cid, "corporation_id": 10}],
                             "cache": "stale", "freshness": {str(cid): {"fetched_at": cid}}})
    client = RemoteEsiClient("http://gateway.test", "x" * 32, opener=opener)
    def read(cid):
        client.get_character_affiliations([cid])
        barrier.wait(timeout=2)
        return client.response_freshness()
    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(read, [1, 2]))
    assert first == {"1": {"fetched_at": 1}}
    assert second == {"2": {"fetched_at": 2}}
    assert client.response_freshness() == {}
    old = RemoteEsiClient("http://gateway.test", "x" * 32,
                          opener=lambda *args, **kwargs: FakeResponse({"data": [], "cache": "hit"}))
    old.get_character_affiliations([1])
    assert old.response_freshness() == {}


def test_remote_throttle_never_bypasses_gateway_with_fallback():
    calls = []
    def opener(*args, **kwargs):
        raise HTTPError("http://gateway.test", 429, "limited", {"Retry-After": "120"}, None)
    fallback = SimpleNamespace(get_character_affiliations=lambda ids: calls.append(ids))
    client = RemoteEsiClient("http://gateway.test", "x" * 32, opener=opener, fallback=fallback)
    with pytest.raises(EsiApiError):
        client.get_character_affiliations([1])
    assert calls == []
