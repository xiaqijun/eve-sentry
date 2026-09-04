import json
from urllib.parse import parse_qs, urlparse

from app.esi.client import EsiClient


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._body


def test_authenticated_esi_requests_send_bearer_token(monkeypatch):
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request)
        if request.full_url.endswith("/location/"):
            return FakeResponse({"solar_system_id": 30002813})
        if request.full_url.endswith("/corporations/456/contacts/"):
            return FakeResponse([{"contact_id": 789, "standing": 5.0}])
        if request.full_url.endswith("/alliances/789/contacts/"):
            return FakeResponse([{"contact_id": 900, "standing": 10.0}])
        if request.full_url.endswith("/characters/123/standings/"):
            return FakeResponse(
                [{"from_id": 456, "from_type": "corporation", "standing": -5.0}]
            )
        if request.full_url.endswith("/characters/affiliation/"):
            return FakeResponse(
                [{"character_id": 123, "corporation_id": 456, "alliance_id": 789}]
            )
        return FakeResponse([{"contact_id": 42, "standing": -10.0}])

    monkeypatch.setattr("app.esi.client.urlopen", fake_urlopen)
    client = EsiClient(base_url="https://esi.test/latest")

    location = client.get_character_location(123, "access-token")
    contacts = client.get_character_contacts(123, "access-token")
    corp_contacts = client.get_corporation_contacts(456, "access-token")
    alliance_contacts = client.get_alliance_contacts(789, "access-token")
    standings = client.get_character_standings(123, "access-token")
    affiliations = client.get_character_affiliations([123])

    assert location == {"solar_system_id": 30002813}
    assert contacts == [{"contact_id": 42, "standing": -10.0}]
    assert corp_contacts == [{"contact_id": 789, "standing": 5.0}]
    assert alliance_contacts == [{"contact_id": 900, "standing": 10.0}]
    assert standings == [{"from_id": 456, "from_type": "corporation", "standing": -5.0}]
    assert affiliations == [{"character_id": 123, "corporation_id": 456, "alliance_id": 789}]
    assert all(
        request.get_header("Authorization") == "Bearer access-token"
        for request in requests[:5]
    )
    assert requests[0].full_url == "https://esi.test/latest/characters/123/location/"
    assert requests[1].full_url == "https://esi.test/latest/characters/123/contacts/"
    assert requests[2].full_url == "https://esi.test/latest/corporations/456/contacts/"
    assert requests[3].full_url == "https://esi.test/latest/alliances/789/contacts/"
    assert requests[4].full_url == "https://esi.test/latest/characters/123/standings/"
    assert requests[5].full_url == "https://esi.test/latest/characters/affiliation/"
    assert json.loads(requests[5].data.decode("utf-8")) == [123]


def test_authenticated_esi_character_search_returns_valid_ids(monkeypatch):
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request)
        return FakeResponse({"character": [123, "456", "bad", 123]})

    monkeypatch.setattr("app.esi.client.urlopen", fake_urlopen)
    client = EsiClient(base_url="https://esi.test/latest")

    result = client.search_characters(99, "access-token", "Long Pilot")

    assert result == [123, 456]
    assert requests[0].get_header("Authorization") == "Bearer access-token"
    parsed = urlparse(requests[0].full_url)
    assert parsed.path == "/latest/characters/99/search/"
    assert parse_qs(parsed.query) == {
        "categories": ["character"],
        "search": ["Long Pilot"],
        "strict": ["false"],
    }
