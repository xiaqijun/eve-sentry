from app.channel_client_gui import (
    ChannelClientConfig,
    create_channel_watcher,
    discover_channel_names,
    load_channel_client_config,
    save_channel_client_config,
)


def test_discover_channel_names_deduplicates_rotated_logs_and_orders_by_activity(tmp_path):
    log_dir = tmp_path / "Chatlogs"
    log_dir.mkdir()
    old = log_dir / "Alliance Intel_20260810_120000.txt"
    newest = log_dir / "Alliance Intel_20260810_120500.txt"
    other = log_dir / "Fleet Intel_20260810_120400.txt"
    old.write_text("old\n", encoding="utf-8")
    newest.write_text("new\n", encoding="utf-8")
    other.write_text("other\n", encoding="utf-8")
    old.touch()
    other.touch()
    newest.touch()

    assert discover_channel_names(log_dir) == ["Alliance Intel", "Fleet Intel"]


def test_channel_client_config_round_trip_does_not_store_credentials(tmp_path):
    config_path = tmp_path / "channel_client_settings.json"
    config = ChannelClientConfig(
        server_url="114.132.167.239:8765",
        log_dir=str(tmp_path / "Chatlogs"),
        selected_channels=[" Alliance Intel ", "Alliance Intel"],
        scan_interval_seconds=99,
        ignore_existing_files=False,
    )

    save_channel_client_config(config, config_path)
    raw = config_path.read_text(encoding="utf-8")
    loaded = load_channel_client_config(config_path)

    assert "api_key" not in raw
    assert loaded.server_url == "http://114.132.167.239:8765"
    assert loaded.selected_channels == ["Alliance Intel"]
    assert loaded.scan_interval_seconds == 10
    assert loaded.ignore_existing_files is False


def test_channel_client_config_uses_safe_defaults_for_invalid_json(tmp_path):
    config_path = tmp_path / "broken.json"
    config_path.write_text("not-json", encoding="utf-8")

    loaded = load_channel_client_config(config_path)

    assert loaded.server_url == ""
    assert loaded.selected_channels == []
    assert loaded.ignore_existing_files is True


def test_first_baseline_is_skipped_but_future_rotated_file_is_read(tmp_path):
    log_dir = tmp_path / "Chatlogs"
    log_dir.mkdir()
    first = log_dir / "Alliance Intel_20260810_120000.txt"
    first.write_text("old history\n", encoding="utf-8")
    state_path = tmp_path / "offsets.json"
    config = ChannelClientConfig(
        log_dir=str(log_dir),
        selected_channels=["Alliance Intel"],
        ignore_existing_files=True,
    )

    watcher = create_channel_watcher(config, state_path)

    assert watcher.poll_lines() == []
    first.write_text("old history\nnew live line\n", encoding="utf-8")
    assert [line.text for line in watcher.poll_lines()] == ["new live line"]

    rotated = log_dir / "Alliance Intel_20260810_130000.txt"
    rotated.write_text("first rotated line\n", encoding="utf-8")
    assert [line.text for line in watcher.poll_lines()] == ["first rotated line"]
