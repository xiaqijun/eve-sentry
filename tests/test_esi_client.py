import json

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
        return FakeResponse([{"contact_id": 42, "standing": -10.0}])

    monkeypatch.setattr("app.esi.client.urlopen", fake_urlopen)
    client = EsiClient(base_url="https://esi.test/latest")

    location = client.get_character_location(123, "access-token")
    contacts = client.get_character_contacts(123, "access-token")
    corp_contacts = client.get_corporation_contacts(456, "access-token")
    alliance_contacts = client.get_alliance_contacts(789, "access-token")

    assert location == {"solar_system_id": 30002813}
    assert contacts == [{"contact_id": 42, "standing": -10.0}]
    assert corp_contacts == [{"contact_id": 789, "standing": 5.0}]
    assert alliance_contacts == [{"contact_id": 900, "standing": 10.0}]
    assert all(
        request.get_header("Authorization") == "Bearer access-token"
        for request in requests
    )
    assert requests[0].full_url == "https://esi.test/latest/characters/123/location/"
    assert requests[1].full_url == "https://esi.test/latest/characters/123/contacts/"
    assert requests[2].full_url == "https://esi.test/latest/corporations/456/contacts/"
    assert requests[3].full_url == "https://esi.test/latest/alliances/789/contacts/"
