"""Runtime read, batch refresh, failure and migration contracts."""

from types import SimpleNamespace

import pytest

from tests.test_personnel_archive import archive_factory  # noqa: F401
from app.esi.personnel_archive import IdentityUpdate, AffiliationUpdate
from app.esi.personnel_runtime import PersonnelRuntime, PersonnelResolver
from app.esi.personnel_policy import classification_profile
from app.esi.personnel_setup import configure_personnel, PersonnelEnricher
from app.esi.resolver import EsiResolver
from app.esi.cache import EsiCache


class Client:
    def __init__(self):
        self.calls = []

    def resolve_ids(self, names):
        self.calls.append(("names", names))
        return {"characters": [{"id": int(name.split()[-1]), "name": name.title()} for name in names]}

    def get_character_affiliations(self, ids):
        self.calls.append(("affiliations", ids))
        return [{"character_id": cid, "corporation_id": 10} for cid in ids]

    def resolve_names(self, ids):
        self.calls.append(("identity", ids))
        return [{"id": cid, "name": f"Renamed {cid}", "category": "character"} for cid in ids]

    def get_corporation(self, cid):
        self.calls.append(("corporation", cid))
        return {"name": "Shared Corp"}


def test_batch_resolution_reuses_archive_without_network_on_reads(archive_factory, tmp_path):
    archive, client = archive_factory(), Client()
    runtime = PersonnelRuntime(archive, client, now=lambda: 1000)
    resolver = PersonnelResolver(EsiResolver(client=client, cache=EsiCache(tmp_path / "legacy.json")), runtime)
    for cid in range(1, 101):
        assert resolver.cached_name(f"Pilot {cid}")[0] is None
    runtime.set_active([(None, f"Pilot {cid}", 1000) for cid in range(1, 101)])
    assert client.calls == []
    runtime.drain_requests()
    assert runtime.run_batch("resolve", realtime=True) == 100
    assert len(client.calls) == 1
    assert runtime.run_batch("affiliation", realtime=True) == 100
    assert len(client.calls) == 2
    assert resolver.cached_name("Pilot 1")[0].entity_id == 1
    assert resolver.character_profile(1)["corporation_id"] == 10
    assert len(client.calls) == 2
    assert archive.get_profiles([1])[1]["last_seen_at"] == 1000
    reopened = PersonnelRuntime(archive_factory(), Client(), now=lambda: 1000)
    reopened.request("Pilot 1")
    reopened.drain_requests()
    assert reopened.lookup("Pilot 1")["character_id"] == 1
    assert reopened.client.calls == []


def test_partial_affiliation_does_not_clear_successful_data(archive_factory):
    archive = archive_factory()
    archive.save_identity(IdentityUpdate(1, "Pilot 1", 100, 100))
    archive.save_affiliation(AffiliationUpdate(1, 10, 100, 20))
    client = Client()
    client.get_character_affiliations = lambda ids: []
    runtime = PersonnelRuntime(archive, client, now=lambda: 1000)
    archive.request_refresh("affiliation", 1, priority=1, due_at=1000)
    runtime.run_batch("affiliation", realtime=True)
    assert archive.get_profiles([1])[1]["alliance_id"] == 20
    assert not archive.claim(now=1001)


def test_upstream_stale_timestamp_is_not_replaced_with_arrival_time(archive_factory):
    archive, client = archive_factory(), Client()
    archive.save_identity(IdentityUpdate(1, "Pilot 1", 100, 100))
    client.response_freshness = lambda: {"1": {"fetched_at": 100, "expires_at": 3000}}
    runtime = PersonnelRuntime(archive, client, now=lambda: 2000)
    archive.request_refresh("affiliation", 1, priority=1, due_at=1000)
    runtime.run_batch("affiliation", realtime=True)
    assert runtime.profile(1)["affiliation_fetched_at"] == 100
    assert runtime.profile(1)["cache_status"] == "stale"
    assert not archive.claim(now=2999)


def test_old_gateway_has_unknown_freshness_and_cannot_confer_trust(archive_factory):
    archive, client = archive_factory(), Client()
    archive.save_identity(IdentityUpdate(1, "Pilot 1", 100, 100))
    client.response_freshness = lambda: {}
    runtime = PersonnelRuntime(archive, client, now=lambda: 2000)
    archive.request_refresh("affiliation", 1, priority=1, due_at=1000)
    runtime.run_batch("affiliation", realtime=True)
    assert runtime.profile(1)["affiliation_trusted"] is False
    assert runtime.profile(1)["corporation_id"] == 10
    assert "corporation_id" not in classification_profile(runtime.profile(1))


def test_runtime_bounds_pending_queue_and_hot_cache(archive_factory):
    runtime = PersonnelRuntime(archive_factory(), Client(), max_hot=2, now=lambda: 1000)
    for cid in (1, 2, 3):
        runtime.request(f"Pilot {cid}")
    assert runtime.snapshot()["pending_names"] == 2
    assert runtime.snapshot()["queue_rejected"] == 1
    runtime.drain_requests()
    runtime.run_batch("resolve", realtime=True)
    assert runtime.snapshot()["hot_profiles"] == 2


def test_runtime_failure_retains_profile_and_sets_degraded(archive_factory):
    archive, client = archive_factory(), Client()
    archive.save_identity(IdentityUpdate(1, "Pilot 1", 100, 100))
    archive.save_affiliation(AffiliationUpdate(1, 10, 100))
    def unavailable(ids):
        raise TimeoutError()
    client.get_character_affiliations = unavailable
    runtime = PersonnelRuntime(archive, client, now=lambda: 1000)
    archive.request_refresh("affiliation", 1, priority=1, due_at=0)
    runtime.run_batch("affiliation", realtime=True)
    assert archive.get_profiles([1])[1]["corporation_id"] == 10
    assert runtime.snapshot()["degraded"]


def test_off_mode_is_zero_io_and_invalid_modes_fail():
    configure_personnel(None, None, None, environment={})
    with pytest.raises(ValueError):
        configure_personnel(None, None, None, environment={"EVE_SENTRY_PERSONNEL_CACHE": "invalid"})
    with pytest.raises(ValueError):
        configure_personnel(None, SimpleNamespace(storage="json"), None, environment={"EVE_SENTRY_PERSONNEL_CACHE": "on"})


def test_contacts_do_not_cross_authorized_contexts():
    tokens = SimpleNamespace(character_id=1, character_owner_hash="owner", scopes=[])
    session = SimpleNamespace(load_tokens=lambda **kwargs: tokens)
    def snapshot(**kwargs):
        return SimpleNamespace(tokens=tokens, contacts=[])
    session.snapshot = snapshot
    enricher = PersonnelEnricher(None, session, now=lambda: 1000)
    assert enricher.refresh_contacts()
    enricher._contact_standings = ["private-context-one"]
    assert enricher.contact_standings() == ["private-context-one"]
    tokens.character_id = 2
    assert enricher.refresh_contacts()
    assert enricher.contact_standings() == []


def test_maintenance_does_not_cancel_retry_after_or_success_schedule(archive_factory):
    archive = archive_factory()
    archive.save_identity(IdentityUpdate(1, "Pilot 1", 100, 100))
    runtime = PersonnelRuntime(archive, Client(), now=lambda: 1000)
    runtime.request("Pilot 1")
    runtime.drain_requests()
    lease = archive.claim(now=1000, kind="affiliation")[0]
    archive.fail(lease, now=1000, error_code="throttled", retry_after=300)
    runtime.maintenance()
    runtime.request("Pilot 1")
    runtime.drain_requests()
    assert not archive.claim(now=1299, kind="affiliation")
    lease = archive.claim(now=1300, kind="affiliation")[0]
    archive.finish(lease, now=1301, next_due_at=5000, next_priority=1,
                   update=AffiliationUpdate(1, 10, 100))
    runtime.maintenance()
    assert not archive.claim(now=4999, kind="affiliation")


def test_hot_profile_lookup_by_id_queues_cold_database_load(archive_factory):
    archive = archive_factory()
    archive.save_identity(IdentityUpdate(1, "Pilot 1", 100, 100))
    runtime = PersonnelRuntime(archive, Client(), now=lambda: 1000)
    assert runtime.profile(1) is None
    runtime.drain_requests()
    assert runtime.profile(1)["name"] == "Pilot 1"
    assert runtime.client.calls == []


def test_runtime_respects_upstream_retry_after(archive_factory):
    from urllib.error import HTTPError
    from app.esi.client import EsiApiError
    client = Client()
    def throttle(ids):
        try:
            raise HTTPError("https://esi.invalid", 429, "throttled", {"Retry-After": "120"}, None)
        except HTTPError as cause:
            raise EsiApiError("throttled") from cause
    client.resolve_ids = throttle
    archive = archive_factory()
    runtime = PersonnelRuntime(archive, client, now=lambda: 1000)
    runtime.request("Pilot 1")
    runtime.drain_requests()
    runtime.run_batch("resolve", realtime=True)
    assert not archive.claim(now=1119)
    assert archive.claim(now=1120)


def test_database_failure_requeues_ids_and_names(archive_factory, monkeypatch):
    archive = archive_factory()
    runtime = PersonnelRuntime(archive, Client(), now=lambda: 1000)
    runtime.profile(1)
    runtime.request("Pilot 1", seen_at=100)
    original = archive.get_profiles
    monkeypatch.setattr(archive, "get_profiles", lambda ids: (_ for _ in ()).throw(TimeoutError()))
    with pytest.raises(TimeoutError):
        runtime.drain_requests()
    assert runtime._pending_ids == {1}
    assert runtime.snapshot()["pending_names"] == 1
    monkeypatch.setattr(archive, "get_profiles", original)
    runtime.drain_requests()
    assert not runtime._pending_ids


def test_read_and_older_observation_do_not_promote_last_seen(archive_factory, tmp_path):
    archive = archive_factory()
    archive.save_identity(IdentityUpdate(1, "Pilot 1", 10, 100))
    runtime = PersonnelRuntime(archive, Client(), now=lambda: 1000)
    resolver = PersonnelResolver(EsiResolver(client=runtime.client, cache=EsiCache(tmp_path / "cache.json")), runtime)
    runtime.request("Pilot 1", seen_at=150)
    runtime.request("Pilot 1", seen_at=50)
    resolver.cached_name("Pilot 1")
    runtime.drain_requests()
    assert archive.get_profiles([1])[1]["last_seen_at"] == 150


def test_missing_affiliation_backfill_stays_cold_until_observed(archive_factory):
    archive = archive_factory()
    archive.save_identity(IdentityUpdate(1, "Pilot 1", 0, 0))
    runtime = PersonnelRuntime(archive, Client(), now=lambda: 1800000000)
    runtime.maintenance()
    assert not archive.claim(now=1800000000, maximum_priority=1)
    runtime.set_active([(1, "Pilot 1", 1800000000)])
    runtime.drain_requests()
    assert archive.claim(now=1800000000, kind="affiliation", maximum_priority=1)


def test_untrusted_organization_standing_does_not_leak_via_flat_metadata():
    from app.server.intel_store import IntelStore
    from app.intel.classification import ClassificationEngine
    store = IntelStore(systems={}, links=[], scorer=ClassificationEngine())
    try:
        profile = {"character_id": 1, "corporation_id": 10, "affiliation_trusted": False,
                   "contact_standing": -10, "standing_contact_type": "corporation"}
        item = {"source": "local_ocr", "name": "Pilot 1", "metadata": {
            "character_profiles": [profile], "contact_standing": -10}}
        assert not store._active_item_is_hostile(item)
        profile["standing_contact_type"] = "character"
        assert store._active_item_is_hostile(item)
    finally:
        store.close()


def test_thousand_names_are_bounded_batches_and_hot_reads_do_not_request(archive_factory):
    import time
    archive, client = archive_factory(), Client()
    runtime = PersonnelRuntime(archive, client, now=lambda: 1800000000)
    runtime.set_active([(None, f"Pilot {cid}", 1800000000) for cid in range(1, 1001)])
    runtime.drain_requests()
    runtime.drain_requests()
    for _ in range(10):
        assert runtime.run_batch("resolve", realtime=True) == 100
    assert len(client.calls) == 10
    assert all(len(names) <= 100 for _, names in client.calls)
    durations = []
    for cid in range(1, 1001):
        started = time.perf_counter()
        assert runtime.lookup(f"Pilot {cid}")["character_id"] == cid
        assert runtime.profile(cid)["character_id"] == cid
        durations.append((time.perf_counter() - started) * 1000)
    assert len(client.calls) == 10
    print(f"\n1000 cached identities: local lookup/profile P95 {sorted(durations)[949]:.3f} ms")
