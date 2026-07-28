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
    _write_log(logs / "Local_1.txt", "Alice")
    _write_log(logs / "Local_2.txt", "Bob")
    store = ClientAuthStateStore(tmp_path / "auth.json", protector=FakeProtector())
    scanner = EveIdentityLogScanner(logs, store)

    first = scanner.scan("eve_key_one")
    assert first.initial_scan is True
    assert first.pending_characters == ["Alice", "Bob"]
    scanner.mark_verified(first.pending_characters)

    _write_log(logs / "Local_1.txt", "Mallory")
    unchanged = scanner.scan("eve_key_one")
    assert unchanged.processed_count == 0
    assert unchanged.pending_characters == []
    assert unchanged.characters == ["Alice", "Bob"]

    _write_log(logs / "Local_3.txt", "Charlie")
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


def test_changing_key_resets_file_index_and_protected_state_hides_secret(tmp_path):
    logs = tmp_path / "Chatlogs"
    logs.mkdir()
    _write_log(logs / "Local_1.txt", "Alice")
    state_path = tmp_path / "auth.json"
    store = ClientAuthStateStore(state_path, protector=FakeProtector())
    scanner = EveIdentityLogScanner(logs, store)

    first = scanner.scan("eve_first_secret")
    scanner.mark_verified(first.pending_characters)
    second = scanner.scan("eve_second_secret")

    assert second.initial_scan is True
    assert second.key_validated is False
    assert second.pending_characters == ["Alice"]
    raw = state_path.read_text(encoding="utf-8")
    assert "eve_first_secret" not in raw
    assert "eve_second_secret" not in raw
