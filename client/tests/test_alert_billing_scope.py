"""Selected account centers define heartbeat billing scope, independently of map hops."""

from copy import deepcopy
from threading import Event, Thread
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.alert_client import AlertClientState, AlertEventWorker, AlertTrayController
from app.intel_client import IntelApiError


class HeartbeatAPI:
    def __init__(self):
        self.calls = []

    def post_heartbeat(self, **payload):
        self.calls.append(deepcopy(payload))
        return {}


@pytest.fixture
def worker(tmp_path, monkeypatch):
    monkeypatch.setattr("app.alert_client.time.monotonic", lambda: 1000.0)
    return AlertEventWorker("https://example.invalid", AlertClientState(tmp_path / "state.json"),
                            client_id="alert-client:test", heartbeat_interval=30)


def scope(api, index=-1):
    return api.calls[index]["details"]["alert_scope"]


def controller_for(worker, accounts):
    controller = AlertTrayController.__new__(AlertTrayController)
    controller._worker = worker
    controller._map_accounts = accounts
    controller.overlay = SimpleNamespace()
    controller._map_worker = None
    controller.requests = []
    controller._start_map_worker = controller.requests.append
    return controller


def test_initial_heartbeat_has_explicit_empty_scope_and_unique_revision(worker, tmp_path):
    api = HeartbeatAPI()
    worker._post_heartbeat(api, "connected")
    assert scope(api)["version"] == 1
    assert scope(api)["systems"] == []
    UUID(scope(api)["revision"])
    another = AlertEventWorker("https://example.invalid", AlertClientState(tmp_path / "other.json"),
                               client_id="alert-client:test")
    other_api = HeartbeatAPI()
    another._post_heartbeat(other_api, "connected")
    assert scope(other_api)["revision"] != scope(api)["revision"]


def test_only_selected_centers_are_billed_and_hops_do_not_change_scope(worker):
    controller = controller_for(worker, [
        {"key": "hajimi1", "system_name": "R-YWID", "system_id": None},
        {"key": "hajimi6", "system_name": "S-KSWL", "system_id": 30003629},
        {"key": "same-center", "system_name": "r-ywid", "system_id": 30003631},
    ])
    api = HeartbeatAPI()
    controller._refresh_local_map(selected_keys=["hajimi1", "same-center"], hops=5)
    worker._post_heartbeat(api, "connected")
    assert scope(api)["systems"] == [{"name": "R-YWID"}]
    revision = scope(api)["revision"]
    controller._refresh_local_map(selected_keys=["same-center", "hajimi1"], hops=1)
    worker._post_heartbeat(api, "connected", force=True)
    assert scope(api)["revision"] == revision
    assert controller.requests[-1]["hops"] == 1
    assert "S-KSWL" not in [item["name"] for item in scope(api)["systems"]]


@pytest.mark.parametrize("accounts,selected", [
    ([{"key": "one", "system_name": "R-YWID"}], []),
    ([{"key": "one", "system_name": "Unknown", "system_id": None}], ["one"]),
    ([], ["offline-account"]),
])
def test_empty_unknown_or_offline_selection_replaces_previous_scope(worker, accounts, selected):
    api = HeartbeatAPI()
    worker.set_alert_scope(["R-YWID"])
    worker._post_heartbeat(api, "connected")
    controller_for(worker, accounts)._refresh_local_map(selected_keys=selected, hops=3)
    worker._post_heartbeat(api, "connected")
    assert len(api.calls) == 2
    assert scope(api)["systems"] == []
    assert scope(api)["revision"] != scope(api, 0)["revision"]


def test_scope_switches_and_return_to_original_get_new_revisions(worker):
    api = HeartbeatAPI()
    for names in (["R-YWID"], ["S-KSWL"], ["R-YWID"]):
        worker.set_alert_scope(names)
        worker._post_heartbeat(api, "connected")
    assert len(api.calls) == 3  # All bypass the unchanged monotonic clock's throttle.
    assert len({scope(api, i)["revision"] for i in range(3)}) == 3
    worker.set_alert_scope([" r-ywid ", "R-YWID"])
    worker._post_heartbeat(api, "connected")
    assert len(api.calls) == 3


def test_unsent_intermediate_selection_still_cuts_the_next_interval(worker):
    api = HeartbeatAPI()
    worker.set_alert_scope(["R-YWID"])
    worker._post_heartbeat(api, "connected")
    worker.set_alert_scope(["S-KSWL"])
    worker.set_alert_scope(["R-YWID"])
    worker._post_heartbeat(api, "connected")
    assert len(api.calls) == 2
    assert scope(api)["systems"] == scope(api, 0)["systems"]
    assert scope(api)["revision"] != scope(api, 0)["revision"]


def test_scope_change_is_sent_on_next_sse_keepalive(worker):
    api = HeartbeatAPI()
    worker.set_alert_scope(["R-YWID"])

    def events(**kwargs):
        yield {"event": "bootstrap", "data": {}, "id": "state:1"}
        worker.set_alert_scope(["S-KSWL"])
        yield {"event": "heartbeat", "data": {}}
        worker.stop()

    api.iter_events = events
    worker.api_factory = lambda *args, **kwargs: api
    worker.run()
    assert len(api.calls) == 2
    assert [scope(api, i)["systems"] for i in range(2)] == [
        [{"name": "R-YWID"}], [{"name": "S-KSWL"}],
    ]


def test_character_moves_without_changing_selected_account(worker):
    account = {"key": "hajimi1", "system_name": "R-YWID"}
    controller = controller_for(worker, [account])
    api = HeartbeatAPI()
    controller._refresh_local_map(selected_keys=["hajimi1"], hops=3)
    worker._post_heartbeat(api, "connected")
    account["system_name"] = "S-KSWL"
    controller._refresh_local_map(selected_keys=["hajimi1"], hops=3)
    worker._post_heartbeat(api, "connected")
    assert scope(api)["systems"] == [{"name": "S-KSWL"}]
    assert scope(api)["revision"] != scope(api, 0)["revision"]


def test_network_send_does_not_lock_out_selection_or_acknowledge_later_revision(worker):
    entered, changed = Event(), Event()
    api = HeartbeatAPI()
    worker.set_alert_scope(["R-YWID"])

    def post_heartbeat(**payload):
        api.calls.append(deepcopy(payload))
        entered.set()
        assert changed.wait(2), "UI scope update blocked behind heartbeat network I/O"

    sender = Thread(target=worker._post_heartbeat,
                    args=(SimpleNamespace(post_heartbeat=post_heartbeat), "connected"))
    sender.start()
    assert entered.wait(2)
    worker.set_alert_scope(["S-KSWL"])
    changed.set()
    sender.join(2)
    assert not sender.is_alive()
    worker._post_heartbeat(api, "connected")
    assert [scope(api, i)["systems"] for i in range(2)] == [
        [{"name": "R-YWID"}], [{"name": "S-KSWL"}],
    ]
    assert scope(api, 0)["revision"] != scope(api, 1)["revision"]


def test_failed_scope_heartbeat_retries_without_normal_interval_delay(worker):
    api = HeartbeatAPI()
    worker._post_heartbeat(api, "connected")
    worker.set_alert_scope(["R-YWID"])

    def fail(**payload):
        raise IntelApiError("temporary failure")

    worker._post_heartbeat(SimpleNamespace(post_heartbeat=fail), "connected")
    worker._post_heartbeat(api, "connected")
    assert len(api.calls) == 2
    assert scope(api)["systems"] == [{"name": "R-YWID"}]


def test_scope_payload_cannot_mutate_worker_snapshot(worker):
    worker.set_alert_scope(["R-YWID"])
    api = HeartbeatAPI()
    captured = []

    def mutate(**payload):
        captured.append(payload["details"]["alert_scope"]["revision"])
        payload["details"]["alert_scope"]["systems"][0]["name"] = "S-KSWL"

    worker._post_heartbeat(SimpleNamespace(post_heartbeat=mutate), "connected")
    worker._post_heartbeat(api, "connected", force=True)
    assert scope(api)["revision"] == captured[0]
    assert scope(api)["systems"] == [{"name": "R-YWID"}]


def test_replacement_worker_restores_current_selection_before_first_heartbeat(worker, monkeypatch):
    controller = controller_for(worker, [{"key": "one", "system_name": "R-YWID"}])
    controller.overlay.map_selection = lambda: (["one"], 3)
    controller.args = SimpleNamespace(server=worker.server, timeout=30, heartbeat_interval=30,
                                      reconnect_max_delay=30, api_key="test-key")
    controller.state = worker.state
    controller.api_factory = HeartbeatAPI
    worker.set_alert_scope(["R-YWID"])
    old_api = HeartbeatAPI()
    worker._post_heartbeat(old_api, "connected")
    new_api = HeartbeatAPI()
    monkeypatch.setattr(AlertEventWorker, "start", lambda current: current._post_heartbeat(new_api, "connected"))
    controller._complete_worker_restart(worker)
    assert scope(new_api)["systems"] == [{"name": "R-YWID"}]
    assert scope(new_api)["revision"] != scope(old_api)["revision"]
