import json

from app.core.client_identity import (
    load_or_create_installation_id,
    persistent_client_id,
)


def test_installation_id_is_created_once_and_reused(tmp_path):
    path = tmp_path / "client_identity.json"

    first = load_or_create_installation_id(path)
    second = load_or_create_installation_id(path)

    assert first == second
    assert len(first) == 32
    assert json.loads(path.read_text(encoding="utf-8"))["installation_id"] == first


def test_client_types_share_installation_id_but_remain_distinct(tmp_path):
    path = tmp_path / "client_identity.json"

    detector = persistent_client_id("detector", path)
    alert = persistent_client_id("alert", path)
    channel = persistent_client_id("channel", path)

    assert detector.startswith("detector-client:")
    assert alert.startswith("alert-client:")
    assert channel.startswith("channel-client:")
    assert len({
        detector.split(":", 1)[1],
        alert.split(":", 1)[1],
        channel.split(":", 1)[1],
    }) == 1
