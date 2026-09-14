"""Missing OCR must not let an icon-only monitor monopolize a system."""

import time

import pytest

from app.server.intel_store import IntelStore
from app.server.source_authority import primary_sources
from tests.test_capture_state import frame


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr("app.server.capture_state.time.time", lambda: clock[0])
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    yield store, clock
    store.close()


def presence(store, client, sequence, count=1, **capture_options):
    return store.record_hostile_presence({
        "client_id": client, "system_name": "S-KSWL", "hostile_icon_count": count,
        "capture": frame(sequence, session=client, **capture_options),
    })


def roster(store, client, sequence, names=None, **capture_options):
    return store.record_ocr_snapshot({
        "client_id": client, "system_name": "S-KSWL",
        "names": ["Enemy Pilot"] if names is None else names,
        "capture": frame(sequence, session=client, **capture_options),
    })


def primary(store):
    return primary_sources(store._active_intel.values())["s-kswl"].metadata["client_id"]


def test_missing_primary_promotes_complete_standby_after_grace_without_oscillation(scenario):
    store, clock = scenario
    presence(store, "first", 1)
    presence(store, "unknown", 1)
    presence(store, "good", 1)
    roster(store, "good", 1)
    clock[0] += 14
    presence(store, "first", 2)
    presence(store, "first", 3)
    assert primary(store) == "first"
    clock[0] += 1
    presence(store, "first", 4)
    assert primary(store) == "good"
    roster(store, "first", 4)
    presence(store, "first", 5)
    assert primary(store) == "good"


@pytest.mark.parametrize("bad_standby", ["none", "empty", "truncated", "changed", "stale", "zero"])
def test_missing_primary_keeps_visual_alert_without_qualified_standby(scenario, bad_standby):
    store, clock = scenario
    presence(store, "first", 1)
    presence(store, "standby", 1)
    if bad_standby != "none":
        roster(store, "standby", 1, names=[] if bad_standby == "empty" else None,
               quality="truncated" if bad_standby == "truncated" else "complete")
    if bad_standby == "changed":
        presence(store, "standby", 2, fingerprint="changed")
    if bad_standby == "zero":
        presence(store, "standby", 2, count=0)
    clock[0] += 46 if bad_standby == "stale" else 16
    presence(store, "first", 2)
    presence(store, "first", 3)
    assert primary(store) == "first"
    assert primary_sources(store._active_intel.values())["s-kswl"].active


def test_retried_frame_is_not_three_independent_frames(scenario):
    store, clock = scenario
    presence(store, "first", 1)
    presence(store, "good", 1)
    roster(store, "good", 1)
    clock[0] += 16
    for _ in range(5):
        presence(store, "first", 1)
    assert primary(store) == "first"
    presence(store, "first", 2)
    assert primary(store) == "first"
    presence(store, "first", 3)
    assert primary(store) == "good"


def test_primary_ocr_recovery_cancels_missing_deadline(scenario):
    store, clock = scenario
    presence(store, "first", 1)
    presence(store, "good", 1)
    roster(store, "good", 1)
    clock[0] += 14
    presence(store, "first", 2)
    roster(store, "first", 2)
    clock[0] += 5
    presence(store, "first", 3)
    assert primary(store) == "first"


def test_zero_and_new_session_do_not_inherit_missing_timer(scenario):
    store, clock = scenario
    presence(store, "first", 1)
    clock[0] += 16
    presence(store, "first", 2)
    presence(store, "first", 3, count=0)
    presence(store, "first", 4)
    meta = primary_sources(store._active_intel.values())["s-kswl"].metadata
    assert meta["roster_missing_since"] == clock[0]
    assert meta["roster_missing_frames"] == 1
    clock[0] += 3
    presence(store, "first", 1, epoch=11)
    meta = primary_sources(store._active_intel.values())["s-kswl"].metadata
    assert meta["roster_missing_since"] == clock[0]
    assert meta["roster_missing_frames"] == 1


def test_empty_ocr_records_failure_without_clearing_presence(scenario):
    store, clock = scenario
    presence(store, "first", 1)
    roster(store, "first", 1, names=[], quality="unknown")
    meta = primary_sources(store._active_intel.values())["s-kswl"].metadata
    assert meta["roster_has_names"] is False
    assert meta["hostile_icon_count"] == 1
    assert meta["roster_missing_frames"] == 1
    presence(store, "good", 1)
    roster(store, "good", 1)
    clock[0] += 16
    presence(store, "first", 2)
    presence(store, "first", 3)
    assert primary(store) == "good"


def test_late_standby_can_take_over_without_another_grace_period(scenario):
    store, clock = scenario
    presence(store, "first", 1)
    clock[0] += 16
    presence(store, "first", 2)
    presence(store, "first", 3)
    presence(store, "good", 1)
    assert primary(store) == "first"
    roster(store, "good", 1)
    assert primary(store) == "good"


def test_query_and_stale_ocr_cannot_qualify_standby(scenario):
    store, clock = scenario
    presence(store, "first", 1)
    presence(store, "good", 2, fingerprint="current")
    stale = roster(store, "good", 1, fingerprint="old")
    assert stale["accepted"] is False
    store.record_ocr_snapshot({"client_id": "good", "system_name": "S-KSWL",
                               "names": ["Enemy Pilot"], "query_id": "manual-query"})
    clock[0] += 16
    presence(store, "first", 2)
    presence(store, "first", 3)
    assert primary(store) == "first"


def test_new_hostile_episode_cannot_reuse_roster_from_before_clear(scenario):
    store, clock = scenario
    presence(store, "first", 1)
    roster(store, "first", 1)
    presence(store, "first", 2, count=0)
    clock[0] += 2
    presence(store, "first", 3)
    meta = primary_sources(store._active_intel.values())["s-kswl"].metadata
    assert meta.get("roster_has_names") is not True
    assert meta["roster_missing_since"] == clock[0]
