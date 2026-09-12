"""Independent source refresh, authority fencing and cached-read integration."""

from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from app.esi.contact_http import ContactRows
from app.esi.organization_relations import PENDING_SOURCE
from app.esi.personnel_setup import PersonnelEnricher
from app.esi.relation_refresh import OrganizationRelations
from tests.test_relation_archive import archive  # noqa: F401


class Client:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.fail = None
        self.own_corporation = 10
        self.own_alliance = 20
        self.values = {"corporation": 5, "alliance": -5}

    def get_character(self, *_):
        self.calls.append("profile")
        if self.fail == "profile":
            raise TimeoutError()
        return {"corporation_id": self.own_corporation, "alliance_id": self.own_alliance}

    def contacts(self, kind):
        self.calls.append(kind)
        if self.fail == kind:
            raise TimeoutError()
        rows = [{"contact_type": "corporation", "contact_id": 30, "standing": self.values[kind]},
                {"contact_type": "character", "contact_id": 99, "standing": 10}]
        return ContactRows(rows, expires_at=self.clock[0] + 126)

    def get_corporation_contacts(self, *_):
        return self.contacts("corporation")

    def get_alliance_contacts(self, *_):
        return self.contacts("alliance")

    def get_character_contacts(self, *_):
        pytest.fail("Organizational classification must not request personal contacts")


@pytest.fixture
def setup(archive):  # noqa: F811 -- Imported pytest fixture.
    clock = [1000.0]
    client = Client(clock)
    tokens = SimpleNamespace(character_id=1, character_owner_hash="owner", access_token="secret", scopes=[
        "esi-corporations.read_contacts.v1", "esi-alliances.read_contacts.v1"])
    session = SimpleNamespace(esi_client=client, load_tokens=lambda **_kwargs: tokens)
    manager = OrganizationRelations(session, archive, now=lambda: clock[0])
    return clock, client, session, manager


def test_only_organization_sources_refresh_and_hot_views_are_reused(setup):
    _, client, _, manager = setup
    assert manager.refresh()
    original = manager.view()
    assert client.calls == ["profile", "corporation", "alliance"]
    for _ in range(1000):
        assert manager.view() is original
        assert manager.view().annotate({"character_id": 99, "corporation_id": 30})["contact_standing"] == 5
    assert not manager.refresh()
    assert client.calls == ["profile", "corporation", "alliance"]


def test_source_failure_does_not_prevent_other_source_success(setup):
    clock, client, _, manager = setup
    manager.refresh()
    client.fail = "corporation"
    client.values["alliance"] = 0
    clock[0] = 1127
    assert manager.refresh()
    corp, alliance = manager.view().sources
    assert corp.successful_at == 1000 and corp.failures == 1
    assert alliance.successful_at == 1127 and alliance.entries[("corporation", 30)] == 0
    assert manager.view().annotate({"corporation_id": 30})["contact_standing"] == 5


def test_first_source_unknown_blocks_lower_source_friendly(setup):
    _, client, _, manager = setup
    client.fail = "corporation"
    client.values["alliance"] = 10
    manager.refresh()
    assert manager.view().sources[1].successful_at == 1000
    assert manager.view().annotate({"corporation_id": 30})["standing_source"] == PENDING_SOURCE


def test_source_retry_after_survives_repeated_refresh_and_restart(setup):
    clock, client, session, manager = setup
    manager.refresh()
    def throttle(*_):
        client.calls.append("throttle")
        raise HTTPError("https://esi.invalid", 429, "", {"Retry-After": "600"}, None)
    client.get_corporation_contacts = throttle
    clock[0] = 1127
    manager.refresh()
    manager = OrganizationRelations(session, manager.repository, now=lambda: clock[0])
    manager.refresh()
    clock[0] = 1400
    manager.refresh()
    assert client.calls.count("throttle") == 1
    assert manager.view().sources[0].retry_at == 1727


@pytest.mark.parametrize("status", [401, 403])
def test_authorization_failure_immediately_quarantines_only_that_source(setup, status):
    clock, client, _, manager = setup
    manager.refresh()
    def denied(*_):
        raise HTTPError("https://esi.invalid", status, "", {}, None)
    client.get_corporation_contacts = denied
    clock[0] = 1127
    manager.refresh()
    assert manager.view().usable_sources == (False, True)
    assert manager.view().annotate({"corporation_id": 30})["standing_source"] == PENDING_SOURCE


def test_downtime_does_not_renew_saved_source_age(setup):
    clock, client, session, manager = setup
    manager.refresh()
    clock[0] = 4599
    client.fail = "corporation"
    restored = OrganizationRelations(session, manager.repository, now=lambda: clock[0])
    restored.refresh()
    assert restored.view().sources[0].successful_at == 1000
    assert restored.view().annotate({"corporation_id": 30})["standing_source"] == PENDING_SOURCE
    old = restored.view()
    clock[0] = 4600
    assert restored.view() is old
    assert restored.view().annotate({"corporation_id": 30})["standing_source"] == PENDING_SOURCE


def test_failed_revocation_write_cannot_restore_friend_after_restart(setup):
    clock, client, session, manager = setup
    manager.refresh()
    original_save = manager.repository.save

    def denied(*_):
        raise HTTPError("https://esi.invalid", 403, "", {}, None)

    client.get_corporation_contacts = denied
    manager.repository.save = lambda *_args, **_kw: (_ for _ in ()).throw(RuntimeError("storage"))
    clock[0] = 1127
    manager.refresh()
    manager.repository.save = original_save
    restored = OrganizationRelations(session, manager.repository, now=lambda: clock[0])
    restored.refresh()
    assert restored.view().annotate({"corporation_id": 30})["standing_source"] == PENDING_SOURCE
    assert not restored.view().sources[0].authorized


def test_new_corporation_isolated_before_failed_relation_fetch(setup):
    clock, client, _, manager = setup
    manager.refresh()
    old = manager.view()
    client.own_corporation = 11
    client.fail = "corporation"
    clock[0] = 1301
    manager.refresh()
    assert manager.view().context != old.context
    assert manager.view().corporation_id == 11
    assert manager.view().annotate({"corporation_id": 30})["standing_source"] == PENDING_SOURCE


def test_login_changed_mid_request_discards_old_result(setup):
    clock, client, session, manager = setup
    manager.refresh()
    clock[0] = 1127
    def switched(*_):
        session.load_tokens = lambda **_kw: SimpleNamespace(character_id=2, character_owner_hash="new", scopes=[])
        return ContactRows([], expires_at=1300)
    client.get_corporation_contacts = switched
    manager.refresh()
    assert not manager.view().own_usable
    assert manager.view().sources == ()


def test_storage_read_failure_can_recover_without_publishing_empty_authority(setup):
    clock, _client, _, manager = setup
    original = manager.repository.load
    manager.repository.load = lambda *_: (_ for _ in ()).throw(RuntimeError("storage"))
    manager.refresh()
    assert manager.view().annotate({"corporation_id": 30})["standing_source"] == PENDING_SOURCE
    manager.repository.load = original
    clock[0] = 1031
    manager.refresh()
    assert manager.view().annotate({"corporation_id": 30})["contact_standing"] == 5


def test_revocation_is_not_hidden_by_failed_persistence(setup):
    clock, client, _, manager = setup
    manager.refresh()
    def denied(*_):
        raise HTTPError("https://esi.invalid", 403, "", {}, None)
    client.get_corporation_contacts = denied
    manager.repository.save = lambda *_args, **_kw: (_ for _ in ()).throw(RuntimeError("storage"))
    clock[0] = 1127
    manager.refresh()
    assert manager.view().annotate({"corporation_id": 30})["standing_source"] == PENDING_SOURCE


def test_enricher_uses_same_view_for_profile_and_snapshot(setup):
    _, client, session, manager = setup
    resolver = SimpleNamespace(character_profile=lambda _: {"character_id": 99, "corporation_id": 30,
                                                          "standing": -10, "standing_contact_type": "character"})
    enricher = PersonnelEnricher(resolver, session, now=manager.now, relations=manager)
    assert enricher.character_profile(99)["standing_source"] == PENDING_SOURCE
    assert enricher.refresh_contacts()
    assert enricher.contact_standings() is manager.view()
    assert enricher.character_profile(99)["contact_standing"] == 5
    assert client.calls == ["profile", "corporation", "alliance"]
