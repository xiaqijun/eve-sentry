import os
import time

import pytest

from app.channels.identity_logs import ClientAuthStateStore, EveIdentityLogScanner


class FakeProtector:
    name = "fake"

    def protect(self, data: bytes) -> bytes:
        return data[::-1]

    def unprotect(self, data: bytes) -> bytes:
        return data[::-1]


def _write_log(path, listener):
    path.write_text(
        "Channel ID: local\n" + (f"Listener: {listener}\n" if listener else ""),
        encoding="utf-8",
    )


def test_initial_scan_reads_all_history_then_only_new_files(tmp_path):
    logs = tmp_path / "Chatlogs"
    logs.mkdir()
    _write_log(logs / "Local_Alice.txt", "Alice")
    _write_log(logs / "Local_Bob.txt", "Bob")
    store = ClientAuthStateStore(tmp_path / "auth.json", protector=FakeProtector())
    scanner = EveIdentityLogScanner(logs, store)

    first = scanner.scan("eve_key_one")
    assert first.initial_scan is True
    assert first.pending_characters == ["Alice", "Bob"]
    scanner.mark_verified(first.pending_characters)

    _write_log(logs / "Local_Alice.txt", "Mallory")
    unchanged = scanner.scan("eve_key_one")
    assert unchanged.processed_count == 0
    assert unchanged.pending_characters == []
    assert unchanged.characters == ["Alice", "Bob"]

    _write_log(logs / "Local_Charlie.txt", "Charlie")
    scanner._next_forced_discovery = 0.0
    added = scanner.scan("eve_key_one")
    assert added.pending_characters == ["Charlie"]
    assert added.processed_count == 1


def test_new_file_without_listener_remains_pending_until_header_is_written(tmp_path):
    logs = tmp_path / "Chatlogs"
    logs.mkdir()
    pending_path = logs / "Local_new.txt"
    _write_log(pending_path, "")
    scanner = EveIdentityLogScanner(
        logs,
        ClientAuthStateStore(tmp_path / "auth.json", protector=FakeProtector()),
    )

    first = scanner.scan("eve_key")
    assert first.key_validated is False
    assert first.pending_files == ["Local_new.txt"]
    assert first.pending_characters == []

    scanner.mark_key_validated()
    validated = scanner.scan("eve_key")
    assert validated.key_validated is True

    _write_log(pending_path, "Alice")
    second = scanner.scan("eve_key")
    assert second.pending_files == []
    assert second.pending_characters == ["Alice"]


def test_pending_file_is_not_reread_until_its_signature_changes(
    tmp_path,
    monkeypatch,
):
    logs = tmp_path / "Chatlogs"
    logs.mkdir()
    pending_path = logs / "Local_pending.txt"
    _write_log(pending_path, "")
    scanner = EveIdentityLogScanner(
        logs,
        ClientAuthStateStore(tmp_path / "auth.json", protector=FakeProtector()),
    )
    reads = []
    original = __import__(
        "app.channels.identity_logs",
        fromlist=["_listener_from_file"],
    )._listener_from_file

    def track_read(path):
        reads.append(path.name)
        return original(path)

    monkeypatch.setattr(
        "app.channels.identity_logs._listener_from_file",
        track_read,
    )

    scanner.scan("eve_key")
    scanner._next_forced_discovery = 0.0
    unchanged = scanner.scan("eve_key")

    assert unchanged.pending_files == [pending_path.name]
    assert reads == [pending_path.name]

    _write_log(pending_path, "Alice")
    changed = scanner.scan("eve_key")

    assert changed.pending_files == []
    assert changed.pending_characters == ["Alice"]
    assert reads == [pending_path.name, pending_path.name]


def test_changing_key_preserves_processed_files_and_protected_state_hides_secret(tmp_path):
    logs = tmp_path / "Chatlogs"
    logs.mkdir()
    _write_log(logs / "Local_Alice.txt", "Alice")
    state_path = tmp_path / "auth.json"
    store = ClientAuthStateStore(state_path, protector=FakeProtector())
    scanner = EveIdentityLogScanner(logs, store)

    first = scanner.scan("eve_first_secret")
    scanner.mark_verified(first.pending_characters)
    first_state = store.load()
    second = scanner.scan("eve_second_secret")

    assert second.initial_scan is False
    assert second.processed_count == 0
    assert second.key_validated is False
    assert second.pending_characters == ["Alice"]
    second_state = store.load()
    assert second_state["processed_files"] == first_state["processed_files"]
    raw = state_path.read_text(encoding="utf-8")
    assert "eve_first_secret" not in raw
    assert "eve_second_secret" not in raw


def test_loading_state_removes_obsolete_listener_queue_fields(tmp_path):
    store = ClientAuthStateStore(
        tmp_path / "auth.json",
        protector=FakeProtector(),
    )
    state = store.empty_state()
    state.update({
        "listener_cursor": {"mtime_ns": 1, "name": "local.txt"},
        "listener_directory_mtime_ns": 1,
        "listener_queue": ["Local_1.txt"],
        "listener_pending_files": {"Local_2.txt": {"mtime_ns": 1}},
    })
    store.save(state)

    loaded = store.load()

    assert "listener_cursor" not in loaded
    assert "listener_directory_mtime_ns" not in loaded
    assert "listener_queue" not in loaded
    assert "listener_pending_files" not in loaded


def test_character_identities_are_normalized_merged_and_preserved_with_key(tmp_path):
    store = ClientAuthStateStore(
        tmp_path / "auth.json",
        protector=FakeProtector(),
    )
    store.set_api_key("eve_first")

    store.remember_character_identities([
        {"character_id": "101", "character_name": "Alice"},
        {"character_id": 202, "character_name": "Bob"},
        {"character_id": "bad", "character_name": "Ignored"},
    ])
    store.remember_character_identities([
        {"character_id": 303, "character_name": "alice"},
    ])

    assert store.load()["character_identities"] == [
        {"character_id": 303, "character_name": "alice"},
        {"character_id": 202, "character_name": "Bob"},
    ]

    store.set_api_key("eve_second")
    assert store.load()["character_identities"] == [
        {"character_id": 303, "character_name": "alice"},
        {"character_id": 202, "character_name": "Bob"},
    ]


def test_initial_scan_reads_only_recent_bounded_logs(tmp_path, monkeypatch):
    logs = tmp_path / "Chatlogs"
    logs.mkdir()
    paths = []
    for index in range(100):
        path = logs / f"Local_{index:03d}.txt"
        _write_log(path, f"Pilot {index:03d}")
        paths.append(path)

    reads = []
    original = __import__(
        "app.channels.identity_logs",
        fromlist=["_listener_from_file"],
    )._listener_from_file

    def track_read(path):
        reads.append(path.name)
        return original(path)

    monkeypatch.setattr(
        "app.channels.identity_logs._listener_from_file",
        track_read,
    )
    scanner = EveIdentityLogScanner(
        logs,
        ClientAuthStateStore(tmp_path / "auth.json", protector=FakeProtector()),
    )

    result = scanner.scan("eve_key")

    assert result.processed_count == 64
    assert result.character_ids == list(range(36, 100))
    assert reads == []

    scanner._discover_entries = lambda: (_ for _ in ()).throw(
        AssertionError("unchanged directory must not be enumerated")
    )
    unchanged = scanner.scan("eve_key")
    assert unchanged.processed_count == 0


def test_scan_uses_recent_local_logs_by_modification_time(tmp_path):
    logs = tmp_path / "Chatlogs"
    logs.mkdir()
    old_local = logs / "Local_20200101_000000_1.txt"
    recent_local = logs / "Local_20260811_000000_2.txt"
    previous_local = logs / "Local_20260810_120000_2.txt"
    recent_intel = logs / "Intel_20260811_000000_2.txt"
    _write_log(old_local, "Old Pilot")
    _write_log(recent_local, "Current Pilot")
    _write_log(previous_local, "Previous Session Pilot")
    _write_log(recent_intel, "Intel Pilot")
    old_time = time.time() - 2 * 24 * 60 * 60
    previous_time = time.time() - 60 * 60
    os.utime(old_local, (old_time, old_time))
    os.utime(previous_local, (previous_time, previous_time))

    scanner = EveIdentityLogScanner(
        logs,
        ClientAuthStateStore(tmp_path / "auth.json", protector=FakeProtector()),
    )

    result = scanner.scan("eve_key")

    assert result.characters == []
    assert result.character_ids == [2]
    assert result.processed_count == 1


def test_scan_groups_english_and_chinese_local_logs_by_character_id(tmp_path):
    logs = tmp_path / "Chatlogs"
    logs.mkdir()
    english_old = logs / "Local_20260811_080000_1001.txt"
    chinese_latest = logs / "本地_20260811_090000_1001.txt"
    other_character = logs / "Local_20260811_100000_2002.txt"
    non_local = logs / "Intel_20260811_110000_3003.txt"
    _write_log(english_old, "Old English Pilot")
    _write_log(chinese_latest, "Current Chinese Pilot")
    _write_log(other_character, "Other Pilot")
    _write_log(non_local, "Intel Pilot")
    now = time.time()
    os.utime(english_old, (now - 30, now - 30))
    os.utime(chinese_latest, (now - 20, now - 20))
    os.utime(other_character, (now - 10, now - 10))

    scanner = EveIdentityLogScanner(
        logs,
        ClientAuthStateStore(tmp_path / "auth.json", protector=FakeProtector()),
    )

    result = scanner.scan("eve_key")

    assert result.characters == []
    assert result.character_ids == [1001, 2002]
    assert result.pending_character_ids == [1001, 2002]
    assert result.processed_count == 2


def test_invalid_api_key_is_not_persisted_or_restored(tmp_path):
    store = ClientAuthStateStore(
        tmp_path / "auth.json",
        protector=FakeProtector(),
    )
    invalid_key = "Vargur\tCargo Hold\nRepublic Fleet Phased Plasma"

    with pytest.raises(ValueError, match="设备密钥格式无效"):
        store.set_api_key(invalid_key)

    assert store.api_key() == ""

    state = store.empty_state()
    state["api_key"] = invalid_key
    store.save(state)

    assert store.api_key() == ""
