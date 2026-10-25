"""Healthy contact refresh must not transiently erase a known hostile roster."""

from urllib.error import HTTPError

import pytest

from app.esi.contact_http import ContactRows
from app.esi.organization_relations import PENDING_SOURCE, STANDING_SOURCE
from tests.test_relation_archive import archive  # noqa: F401
from tests.test_relation_refresh import setup  # noqa: F401


@pytest.mark.parametrize("kind", ["corporation", "alliance"])
def test_contact_refresh_finishes_before_previous_snapshot_expires(setup, kind):
    clock, client, _, manager = setup
    # A neutral corporation still needs both organization sources to be known.
    client.values = {"corporation": 0, "alliance": 0}
    manager.refresh()
    old_deadline = 1126.0
    original = getattr(client, f"get_{kind}_contacts")
    observed = []

    def slow_contacts(*args):
        observed.append(manager.view().annotate({"corporation_id": 40})["standing_source"])
        clock[0] += 4
        observed.append(manager.view().annotate({"corporation_id": 40})["standing_source"])
        return original(*args)

    setattr(client, f"get_{kind}_contacts", slow_contacts)
    clock[0] = old_deadline - 31
    manager.refresh()
    assert client.calls.count(kind) == 1
    clock[0] = old_deadline - 30
    manager.refresh()
    assert observed == [STANDING_SOURCE, STANDING_SOURCE]
    assert client.calls.count(kind) == 2
    clock[0] = old_deadline + 1
    profile = manager.view().annotate({"corporation_id": 40})
    assert profile["standing_source"] == STANDING_SOURCE
    assert profile["contact_standing"] == 0


def test_early_refresh_failure_keeps_only_the_original_validity_window(setup):
    clock, client, _, manager = setup
    manager.refresh()
    client.fail = "corporation"
    clock[0] = 1096
    manager.refresh()
    source = manager.view().sources[0]
    assert source.failures == 1
    assert source.successful_at == 1000
    assert source.expires_at == 1126
    assert manager.view().annotate({"corporation_id": 30})["contact_standing"] == 5
    calls = list(client.calls)
    clock[0] = 1100
    manager.refresh()
    assert client.calls == calls
    clock[0] = 1126
    assert manager.view().annotate({"corporation_id": 30})["standing_source"] == PENDING_SOURCE


@pytest.mark.parametrize("status", [401, 403, 429])
def test_early_refresh_respects_revocation_and_retry_after(setup, status):
    clock, client, _, manager = setup
    manager.refresh()

    def rejected(*_):
        client.calls.append("rejected")
        raise HTTPError("https://esi.invalid", status, "", {"Retry-After": "600"}, None)

    client.get_corporation_contacts = rejected
    clock[0] = 1096
    manager.refresh()
    source = manager.view().sources[0]
    assert source.retry_at == 1696
    assert source.authorized is (status == 429)
    profile = manager.view().annotate({"corporation_id": 30})
    assert profile["standing_source"] == (STANDING_SOURCE if status == 429 else PENDING_SOURCE)
    clock[0] = 1127
    manager.refresh()
    assert client.calls.count("rejected") == 1
    assert manager.view().annotate({"corporation_id": 30})["standing_source"] == PENDING_SOURCE


def test_revalidated_aged_response_never_extends_expiry_or_spins(setup):
    clock, client, _, manager = setup
    manager.refresh()
    original = client.get_corporation_contacts

    def aged_contacts(*args):
        rows = original(*args)
        return ContactRows(rows, expires_at=1126)

    client.get_corporation_contacts = aged_contacts
    clock[0] = 1096
    manager.refresh()
    source = manager.view().sources[0]
    assert source.expires_at == 1126
    assert source.retry_at >= 1101
    calls = list(client.calls)
    for _ in range(20):
        manager.refresh()
    assert client.calls == calls
    clock[0] = 1126
    assert manager.view().annotate({"corporation_id": 30})["standing_source"] == PENDING_SOURCE
