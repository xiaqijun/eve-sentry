"""Session/frame fences and independent-frame quality takeover."""

from app.server.capture_state import (
    capture_is_current,
    capture_payload,
    record_roster_quality,
)
from app.server.intel_store import IntelStore
from app.server.source_authority import primary_sources


def frame(
    seq, *, epoch=10, session="session-a", fingerprint="same", quality="complete"
):
    return {
        "session_epoch": epoch,
        "session_id": session,
        "sequence": seq,
        "fingerprint": fingerprint,
        "roster_quality": quality,
    }


def publish(store, client, capture):
    return store.record_hostile_presence(
        {
            "client_id": client,
            "system_name": "S-KSWL",
            "hostile_icon_count": 1,
            "capture": capture,
        }
    )


def test_fences_reject_old_sessions_legacy_bypass_and_changed_ocr_frame(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    publish(store, "node", frame(1))
    publish(store, "node", frame(3))
    items = store._active_intel.values()
    assert capture_is_current(items, "node", frame(1), ocr=True)
    assert not capture_is_current(items, "node", frame(2))
    assert not capture_is_current(
        items, "node", frame(3, fingerprint="different"), ocr=True
    )
    assert not capture_is_current(items, "node", {})
    assert publish(store, "node", frame(1, epoch=11, session="new"))["accepted"]
    assert not publish(store, "node", frame(100))["accepted"]
    store.close()


def test_three_independent_truncations_select_good_standby_not_unknown(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    for client in ("primary", "unknown", "good"):
        publish(store, client, frame(1, session=client))
    items = store._active_intel.values()
    record_roster_quality(items, "good", "S-KSWL", frame(1))
    for seq in (1, 1, 1, 2):
        record_roster_quality(
            items, "primary", "S-KSWL", frame(seq, quality="truncated")
        )
    assert primary_sources(items)["s-kswl"].metadata["client_id"] == "primary"
    changed = record_roster_quality(
        items, "primary", "S-KSWL", frame(3, quality="truncated")
    )
    assert {item.metadata["client_id"] for item in changed} == {"primary", "unknown"}
    assert primary_sources(items)["s-kswl"].metadata["client_id"] == "good"
    record_roster_quality(items, "primary", "S-KSWL", frame(4))
    assert primary_sources(items)["s-kswl"].metadata["client_id"] == "good"
    store.close()


def test_truncated_without_qualified_standby_keeps_visual_source(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    publish(store, "primary", frame(1))
    items = store._active_intel.values()
    for seq in range(1, 5):
        record_roster_quality(
            items, "primary", "S-KSWL", frame(seq, quality="truncated")
        )
    assert primary_sources(items)["s-kswl"].metadata["client_id"] == "primary"
    store.close()


def test_capture_validation_rejects_boolean_and_out_of_range_sequence():
    import pytest

    for invalid in (True, 0, -1, 2**63, "1"):
        with pytest.raises(ValueError):
            capture_payload({"capture": frame(invalid)})
