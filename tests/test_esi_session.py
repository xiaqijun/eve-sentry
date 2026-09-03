import base64
import json

import pytest

from app.esi.session import (
    ContactStanding,
    EsiAuthenticatedSession,
    EsiStanding,
    SEARCH_SCOPE,
    apply_contact_standing,
    contact_standings_from_payload,
    matching_contact_standing,
    standings_from_payload,
)
from app.esi.sso import EsiSsoError, EsiTokenStore, TokenSet


def jwt(payload):
    def encode(value):
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return f"{encode({'alg': 'none'})}.{encode(payload)}.sig"


def test_contact_standings_normalize_payload_and_skip_invalid_rows():
    contacts = contact_standings_from_payload(
        [
            {
                "contact_id": 123,
                "contact_type": "character",
                "standing": -10,
                "label": "bad",
            },
            {"contact_id": "", "standing": 5},
            {"contact_id": 456, "standing": "not-a-number"},
            "ignored",
        ]
    )

    assert len(contacts) == 1
    assert contacts[0].contact_id == 123
    assert contacts[0].contact_type == "character"
    assert contacts[0].standing == -10.0
    assert contacts[0].to_dict()["source"] == "esi_contacts"


def test_character_standings_normalize_complete_snapshot():
    standings = standings_from_payload(
        [
            {"from_id": 456, "from_type": "corporation", "standing": -5},
            {"from_id": 789, "from_type": "alliance", "standing": "7.5"},
            {"from_id": "bad", "from_type": "agent", "standing": 1},
        ]
    )

    assert standings == [
        EsiStanding(from_id=456, from_type="corporation", standing=-5.0),
        EsiStanding(from_id=789, from_type="alliance", standing=7.5),
    ]


def test_contact_standing_matching_prefers_character_then_corporation():
    contacts = contact_standings_from_payload(
        [
            {"contact_id": 789, "contact_type": "alliance", "standing": -2},
            {"contact_id": 456, "contact_type": "corporation", "standing": -5},
            {"contact_id": 123, "contact_type": "character", "standing": -10},
        ]
    )
    profile = {
        "character_id": 123,
        "corporation_id": 456,
        "alliance_id": 789,
    }

    match = matching_contact_standing(profile, contacts)
    annotated = apply_contact_standing(profile, contacts)

    assert match is not None
    assert match.contact_type == "character"
    assert annotated["contact_standing"] == -10.0
    assert annotated["standing_contact_id"] == 123
    assert annotated["standing_contact_type"] == "character"


def test_authenticated_session_refreshes_and_fetches_snapshot(tmp_path):
    old_access = jwt({"sub": "CHARACTER:EVE:123", "owner": "owner-old"})
    new_access = jwt(
        {
            "sub": "CHARACTER:EVE:123",
            "owner": "owner-new",
            "scp": [
                "esi-location.read_location.v1",
                "esi-characters.read_contacts.v1",
            ],
        }
    )
    store = EsiTokenStore(tmp_path / "tokens.json")
    store.save(
        TokenSet.from_payload(
            {
                "access_token": old_access,
                "expires_at": 900,
                "refresh_token": "refresh-old",
            }
        )
    )

    class FakeSso:
        def __init__(self):
            self.refresh_calls = []

        def refresh(self, refresh_token, scopes=None):
            self.refresh_calls.append((refresh_token, scopes))
            return TokenSet.from_payload(
                {
                    "access_token": new_access,
                    "expires_in": 1200,
                    "refresh_token": "",
                },
                now=lambda: 1000.0,
            )

    class FakeEsi:
        def __init__(self):
            self.calls = []

        def get_character_location(self, character_id, access_token):
            self.calls.append(("location", character_id, access_token))
            return {"solar_system_id": 30002813}

        def get_character_contacts(self, character_id, access_token):
            self.calls.append(("contacts", character_id, access_token))
            return [
                {
                    "contact_id": 456,
                    "contact_type": "corporation",
                    "standing": -10,
                }
            ]

    sso = FakeSso()
    esi = FakeEsi()
    session = EsiAuthenticatedSession(
        sso_client=sso,
        esi_client=esi,
        token_store=store,
        now=lambda: 1000.0,
    )

    snapshot = session.snapshot()
    saved = store.load()

    assert sso.refresh_calls == [("refresh-old", None)]
    assert snapshot.tokens.access_token == new_access
    assert snapshot.tokens.refresh_token == "refresh-old"
    assert snapshot.tokens.character_owner_hash == "owner-new"
    assert snapshot.location == {"solar_system_id": 30002813}
    assert snapshot.contacts[0].contact_id == 456
    assert snapshot.contacts[1].contact_id == 123
    assert snapshot.contacts[1].standing == 10.0
    assert snapshot.contacts[1].source == "esi_self"
    assert snapshot.to_dict()["contacts"][0]["standing"] == -10.0
    assert esi.calls == [
        ("location", 123, new_access),
        ("contacts", 123, new_access),
    ]
    assert saved is not None
    assert saved.refresh_token == "refresh-old"


def test_authenticated_session_fetches_corporation_and_alliance_contacts(tmp_path):
    access = jwt(
        {
            "sub": "CHARACTER:EVE:123",
            "owner": "owner",
            "scp": [
                "esi-characters.read_contacts.v1",
                "esi-corporations.read_contacts.v1",
                "esi-alliances.read_contacts.v1",
            ],
        }
    )
    store = EsiTokenStore(tmp_path / "tokens.json")
    store.save(
        TokenSet.from_payload(
            {
                "access_token": access,
                "expires_at": 2000,
                "refresh_token": "refresh",
            }
        )
    )

    class FakeEsi:
        def __init__(self):
            self.calls = []

        def get_character_contacts(self, character_id, access_token):
            self.calls.append(("character_contacts", character_id, access_token))
            return [{"contact_id": 321, "contact_type": "character", "standing": -5}]

        def get_character(self, character_id):
            self.calls.append(("character", character_id))
            return {"corporation_id": 456, "alliance_id": 789}

        def get_corporation_contacts(self, corporation_id, access_token):
            self.calls.append(("corporation_contacts", corporation_id, access_token))
            return [{"contact_id": 987, "contact_type": "alliance", "standing": 5}]

        def get_alliance_contacts(self, alliance_id, access_token):
            self.calls.append(("alliance_contacts", alliance_id, access_token))
            return [{"contact_id": 654, "contact_type": "corporation", "standing": 10}]

    esi = FakeEsi()
    session = EsiAuthenticatedSession(
        sso_client=object(),
        esi_client=esi,
        token_store=store,
        now=lambda: 1000.0,
    )

    snapshot = session.snapshot(include_location=False)

    assert [(item.contact_id, item.contact_type, item.standing) for item in snapshot.contacts] == [
        (321, "character", -5.0),
        (987, "alliance", 5.0),
        (654, "corporation", 10.0),
        (123, "character", 10.0),
        (456, "corporation", 10.0),
        (789, "alliance", 10.0),
    ]
    assert esi.calls == [
        ("character_contacts", 123, access),
        ("character", 123),
        ("corporation_contacts", 456, access),
        ("alliance_contacts", 789, access),
    ]


def test_self_standing_overrides_matching_contact_entry():
    contacts = contact_standings_from_payload(
        [{"contact_id": 123, "contact_type": "character", "standing": -10}]
    )
    contacts.append(
        ContactStanding(
            contact_id=123,
            contact_type="character",
            standing=10.0,
            source="esi_self",
        )
    )
    profile = {"character_id": 123}

    annotated = apply_contact_standing(profile, contacts)

    assert annotated["contact_standing"] == 10.0
    assert annotated["standing_source"] == "esi_self"


def test_authenticated_session_requires_saved_tokens(tmp_path):
    session = EsiAuthenticatedSession(
        sso_client=object(),
        token_store=EsiTokenStore(tmp_path / "missing.json"),
    )

    with pytest.raises(EsiSsoError):
        session.load_tokens()


def test_authenticated_session_caches_standings_and_uses_stale_on_failure(tmp_path):
    access = jwt(
        {
            "sub": "CHARACTER:EVE:123",
            "owner": "owner",
            "scp": ["esi-characters.read_standings.v1"],
        }
    )
    store = EsiTokenStore(tmp_path / "tokens.json")
    store.save(
        TokenSet.from_payload(
            {
                "access_token": access,
                "expires_at": 2000,
                "refresh_token": "refresh",
            }
        )
    )

    class FakeCache:
        def __init__(self):
            self.values = {}
            self.stale = {}
            self.set_calls = []

        def get(self, key):
            return self.values.get(key)

        def get_stale(self, key):
            return self.stale.get(key, self.values.get(key))

        def set(self, key, value, ttl_seconds):
            self.set_calls.append((key, ttl_seconds))
            self.values[key] = value
            self.stale[key] = value

        def save(self):
            return None

    class FakeEsi:
        def __init__(self):
            self.calls = 0
            self.fail = False

        def get_character_standings(self, character_id, access_token):
            self.calls += 1
            if self.fail:
                raise RuntimeError("temporary ESI failure")
            return [{"from_id": 456, "from_type": "corporation", "standing": -5}]

    cache = FakeCache()
    esi = FakeEsi()
    session = EsiAuthenticatedSession(
        sso_client=object(),
        esi_client=esi,
        token_store=store,
        cache=cache,
        now=lambda: 1000.0,
    )

    first = session.standings()
    second = session.standings()
    assert first == second == [
        EsiStanding(from_id=456, from_type="corporation", standing=-5.0)
    ]
    assert esi.calls == 1
    assert cache.set_calls == [("standings:character:123", 600)]

    session._standings_memory.clear()
    cache.values.clear()
    esi.fail = True
    assert session.standings() == first


def test_authenticated_session_completes_unique_character_prefix(tmp_path):
    access = jwt(
        {
            "sub": "CHARACTER:EVE:123",
            "scp": [SEARCH_SCOPE],
        }
    )
    store = EsiTokenStore(tmp_path / "tokens.json")
    store.save(
        TokenSet.from_payload(
            {
                "access_token": access,
                "expires_at": 2000,
                "refresh_token": "refresh",
            }
        )
    )

    class FakeEsi:
        def __init__(self):
            self.calls = []

        def search_characters(self, character_id, access_token, search):
            self.calls.append(("search", character_id, access_token, search))
            return [456, 789]

        def resolve_names(self, character_ids):
            self.calls.append(("names", list(character_ids)))
            return [
                {
                    "id": 456,
                    "name": "Kamamdzhava Tekerav Longname",
                    "category": "character",
                },
                {
                    "id": 789,
                    "name": "Unrelated Pilot",
                    "category": "character",
                },
            ]

    esi = FakeEsi()
    session = EsiAuthenticatedSession(
        sso_client=object(),
        esi_client=esi,
        token_store=store,
        now=lambda: 1000.0,
    )

    result = session.complete_character_name("Kamamdzhava Teker")

    assert result == "Kamamdzhava Tekerav Longname"
    assert esi.calls == [
        ("search", 123, access, "Kamamdzhava Teker"),
        ("names", [456, 789]),
    ]


def test_authenticated_session_rejects_ambiguous_character_prefix(tmp_path):
    access = jwt({"sub": "CHARACTER:EVE:123", "scp": [SEARCH_SCOPE]})
    store = EsiTokenStore(tmp_path / "tokens.json")
    store.save(
        TokenSet.from_payload(
            {
                "access_token": access,
                "expires_at": 2000,
                "refresh_token": "refresh",
            }
        )
    )

    class FakeEsi:
        def search_characters(self, character_id, access_token, search):
            return [456, 789]

        def resolve_names(self, character_ids):
            return [
                {"name": "Kamamdzhava Tekerav One", "category": "character"},
                {"name": "Kamamdzhava Tekerav Two", "category": "character"},
            ]

    session = EsiAuthenticatedSession(
        sso_client=object(),
        esi_client=FakeEsi(),
        token_store=store,
        now=lambda: 1000.0,
    )

    assert session.complete_character_name("Kamamdzhava Teker") is None
