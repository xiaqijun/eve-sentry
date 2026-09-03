import json
from pathlib import Path

from dto.python.eve_sentry_contracts.dto import (
    BootstrapPayload,
    HeartbeatResponse,
    HostileMovementEvent,
    OcrQueryCreateRequest,
    OcrQueryResult,
    OcrQueryTask,
    SystemRef,
)


ROOT = Path(__file__).parent


def load_fixture(name: str) -> dict:
    return json.loads((ROOT / "fixtures" / name).read_text(encoding="utf-8"))


def test_bootstrap_v1_fixture_and_authoritative_count() -> None:
    payload = load_fixture("bootstrap.json")
    dto = BootstrapPayload.from_dict(payload)
    assert dto.schema_version == "intel_bootstrap.v1"
    assert dto.map["systems"][0]["hostile_count"] == 2
    assert dto.monitoring_nodes_version == "nodes-v1"
    assert dto.hostile_personnel == []


def test_hostile_movement_is_explicit_and_reconcilable() -> None:
    payload = load_fixture("hostile_movement.json")
    event = HostileMovementEvent(
        movement_id=payload["movement_id"],
        occurred_at=payload["occurred_at"],
        from_system=SystemRef(**payload["from_system"]),
        to_system=SystemRef(**payload["to_system"]),
        hostile_count=payload["hostile_count"],
        personnel=tuple(payload["personnel"]),
        source=payload["source"],
    )
    assert event.schema_version == "hostile_movement_event.v1"
    assert event.from_system.name != event.to_system.name
    assert event.hostile_count >= 1


def test_gateway_health_counter_invariants() -> None:
    payload = load_fixture("esi_gateway_health.json")
    assert payload["service"] == "eve-sentry-esi-gateway"
    assert payload["cache_hits"] + payload["cache_misses"] == payload["requests"]
    assert 0 <= payload["cache_hit_rate"] <= 1


def test_sse_compatibility_policy_is_additive() -> None:
    allowed = {"bootstrap", "alert", "safe", "monitoring_node", "hostile_movement"}
    assert "hostile_movement" in allowed
    assert "monitoring_node" in allowed


def test_one_time_ocr_query_create_is_idempotent_and_multi_window() -> None:
    payload = load_fixture("ocr_query_create.json")
    dto = OcrQueryCreateRequest.from_dict(payload)
    assert dto.request_id == "ocr-req-20260903-0001"
    assert dto.query_type == "all"
    assert dto.window_ids == ("main", "alt-1")
    assert not hasattr(dto, "future_field")


def test_heartbeat_capture_command_supports_all_query_types() -> None:
    response = HeartbeatResponse.from_dict(load_fixture("ocr_query_heartbeat.json"))
    assert {command.query_type for command in response.commands} == {"all", "person", "corporation", "alliance"}
    assert all(command.command == "capture_ocr_once" for command in response.commands)
    assert response.commands[1].window_ids == ("main", "alt-1")


def test_one_time_ocr_result_accepts_unknown_fields_and_window_identity() -> None:
    result = OcrQueryResult.from_dict(load_fixture("ocr_query_result.json"))
    assert result.request_id == "ocr-req-20260903-0001"
    assert result.window_id == "alt-1"
    assert [item.name for item in result.names] == ["Jane Doe", "Example Pilot"]


def test_query_status_exposes_partial_timeout_and_client_failure() -> None:
    task = OcrQueryTask.from_dict(load_fixture("ocr_query_status_partial.json"))
    assert task.status == "partial"
    assert task.expires_at.endswith("30Z")
    assert {node.status for node in task.nodes} == {"captured", "timeout", "ocr_failed"}
    assert task.result is not None
    assert task.result.timed_out_nodes == 1
    assert task.result.failed_nodes == 1


def test_ocr_schema_and_openapi_are_additive_and_cover_protocol() -> None:
    schema = json.loads((ROOT.parent / "schemas" / "ocr-query.schema.json").read_text(encoding="utf-8"))
    assert schema["$schema"].endswith("draft/2020-12/schema")
    assert schema["$defs"]["query_type"]["enum"] == ["all", "person", "corporation", "alliance"]
    assert schema["$defs"]["capture_ocr_once"]["properties"]["command"]["const"] == "capture_ocr_once"
    assert all(definition.get("additionalProperties") is True for definition in schema["$defs"].values() if definition.get("type") == "object")
    openapi = (ROOT.parent / "openapi" / "eve-sentry.openapi.yaml").read_text(encoding="utf-8")
    for marker in ("/api/v1/ocr/queries:", "/api/v1/ocr/queries/{request_id}:", "/api/v1/ocr/queries/{request_id}/result:", "CaptureOcrOnceCommand", "HeartbeatResponse"):
        assert marker in openapi
