"""Regression coverage for private contact failure, expiry and authority fences."""

from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from app.esi.contact_http import ContactReadError, ContactRows
from app.esi.personnel_setup import PersonnelEnricher
from app.esi.session import EsiAuthenticatedSession, matching_contact_standing
from app.esi.sso import TokenSet


class ContactsClient:
    def __init__(self, clock):
        self.clock = clock
        self.fail = None
        self.corporation = 10
        self.alliance = 20
        self.empty = False

    def rows(self, source, cid, kind="corporation"):
        if self.fail == source:
            raise TimeoutError("private upstream details must not be logged")
        return ContactRows([] if self.empty else [{"contact_id": cid, "contact_type": kind, "standing": 5}],
                           expires_at=self.clock[0] + 126)

    def get_character_contacts(self, *_):
        return self.rows("personal", 50, "character")

    def get_character(self, *_):
        if self.fail == "profile":
            raise TimeoutError()
        result = {"corporation_id": self.corporation}
        if self.alliance:
            result["alliance_id"] = self.alliance
        return result

    def get_corporation_contacts(self, *_):
        return self.rows("corporation", 60)

    def get_alliance_contacts(self, *_):
        return self.rows("alliance", 70, "alliance")


@pytest.fixture
def setup():
    clock = [1000.0]
    client = ContactsClient(clock)
    tokens = TokenSet.from_payload({"access_token": "test-token", "expires_at": 100000,
                                   "character_id": 1, "character_owner_hash": "owner", "scopes": [
        "esi-characters.read_contacts.v1", "esi-corporations.read_contacts.v1", "esi-alliances.read_contacts.v1"]})
    store = SimpleNamespace(load=lambda: tokens)
    session = EsiAuthenticatedSession(object(), client, store, now=lambda: clock[0])
    enricher = PersonnelEnricher(None, session, now=lambda: clock[0])
    return clock, client, session, enricher


@pytest.mark.parametrize("source", ["personal", "corporation", "alliance", "profile"])
def test_failed_source_cannot_replace_successful_relationships(setup, source):
    clock, client, _session, enricher = setup
    assert enricher.refresh_contacts()
    previous = enricher.contact_standings()
    assert matching_contact_standing({"corporation_id": 60}, previous).standing == 5
    client.fail = source
    clock[0] = 1127
    assert not enricher.refresh_contacts()
    assert enricher.contact_standings() == previous
    assert enricher._successful_at == 1000
    assert 1157 <= enricher._contact_standings_until <= 1160
    assert enricher._contacts_status == "degraded"


def test_successful_empty_table_really_revokes_contacts(setup):
    clock, client, _, enricher = setup
    enricher.refresh_contacts()
    client.empty = True
    clock[0] = 1127
    assert enricher.refresh_contacts()
    assert {entry.contact_id for entry in enricher.contact_standings()} == {1, 10, 20}


def test_deadline_uses_remaining_official_lifetime(setup):
    _, _, _, enricher = setup
    enricher.refresh_contacts()
    assert enricher._contact_standings_until == 1126


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_is_immediately_isolated(setup, status):
    clock, client, _, enricher = setup
    enricher.refresh_contacts()
    def denied(*_):
        raise HTTPError("https://esi.invalid", status, "denied", {}, None)
    client.get_corporation_contacts = denied
    clock[0] = 1127
    assert enricher.refresh_contacts()
    assert enricher.contact_standings() == []
    assert enricher._contacts_status == "unavailable"
    assert enricher._successful_at is None


def test_retry_after_is_not_cancelled_by_frequent_refresh(setup):
    clock, client, _, enricher = setup
    enricher.refresh_contacts()
    calls = []
    def throttled(*_):
        calls.append(1)
        raise HTTPError("https://esi.invalid", 429, "throttled", {"Retry-After": "600"}, None)
    client.get_corporation_contacts = throttled
    clock[0] = 1127
    enricher.refresh_contacts()
    assert enricher._contact_standings_until == 1727
    for timestamp in (1130, 1200, 1726):
        clock[0] = timestamp
        enricher.refresh_contacts()
    assert calls == [1]
    assert enricher._successful_at == 1000


def test_changed_organization_cannot_fall_back_to_old_organization(setup):
    clock, client, _, enricher = setup
    enricher.refresh_contacts()
    client.corporation = 11
    client.fail = "corporation"
    clock[0] = 1127
    assert enricher.refresh_contacts()
    assert enricher.contact_standings() == []


def test_failure_retention_is_bounded_without_network_in_hot_reads(setup):
    clock, client, _, enricher = setup
    enricher.refresh_contacts()
    client.fail = "corporation"
    clock[0] = 4599
    assert enricher.contact_standings()
    clock[0] = 4600
    assert enricher.contact_standings() == []
    assert enricher.refresh_contacts()
    assert enricher._successful_at is None


def test_old_authority_result_is_fenced_even_when_request_succeeds(setup):
    _, client, session, enricher = setup
    original = client.get_alliance_contacts
    def replaced(*args):
        result = original(*args)
        session.token_store.load = lambda: SimpleNamespace(character_id=2, character_owner_hash="new", scopes=[])
        return result
    client.get_alliance_contacts = replaced
    assert enricher.refresh_contacts()
    assert enricher.contact_standings() == []


def test_no_alliance_does_not_create_empty_alliance_self_relation(setup):
    _, client, _, enricher = setup
    client.alliance = None
    enricher.refresh_contacts()
    assert all(entry.contact_type != "alliance" for entry in enricher.contact_standings())


def test_missing_org_method_does_not_mean_empty_table(setup):
    _, _, session, _ = setup
    with pytest.raises(ContactReadError):
        session._optional_contacts("unsupported_source", 10, "test")


def test_bad_own_profile_is_not_success(setup):
    _, client, session, _ = setup
    client.get_character = lambda *_: {}
    with pytest.raises(ContactReadError):
        session.contacts_snapshot()


def test_expired_response_never_extends_success_time(setup):
    clock, client, _, enricher = setup
    enricher.refresh_contacts()
    previous = enricher.contact_standings()
    clock[0] = 1127
    client.get_character_contacts = lambda *_: ContactRows([], expires_at=1100)
    assert not enricher.refresh_contacts()
    assert enricher.contact_standings() == previous
    assert enricher._successful_at == 1000


def test_failure_after_authority_switch_also_discards_old_data(setup):
    clock, client, session, enricher = setup
    enricher.refresh_contacts()
    clock[0] = 1127
    def replaced_then_failed(*_):
        session.token_store.load = lambda: SimpleNamespace(character_id=2, character_owner_hash="new", scopes=[])
        raise TimeoutError()
    client.get_character_contacts = replaced_then_failed
    assert enricher.refresh_contacts()
    assert enricher.contact_standings() == []


def test_refresh_does_not_block_hot_reads_or_start_a_second_request(setup):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    clock, client, _, enricher = setup
    enricher.refresh_contacts()
    previous = enricher.contact_standings()
    clock[0] = 1127
    started, release = threading.Event(), threading.Event()
    def blocked(*_):
        started.set()
        assert release.wait(3)
        raise TimeoutError()
    client.get_character_contacts = blocked
    with ThreadPoolExecutor(max_workers=2) as pool:
        refresh = pool.submit(enricher.refresh_contacts)
        try:
            assert started.wait(2)
            assert pool.submit(enricher.contact_standings).result(timeout=1) == previous
            assert pool.submit(enricher.refresh_contacts).result(timeout=1) is False
        finally:
            release.set()
        assert refresh.result(timeout=2) is False
