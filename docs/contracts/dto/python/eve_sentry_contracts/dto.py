"""Small dependency-free DTOs; unknown wire fields are intentionally preserved."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Level = Literal["low", "medium", "high", "critical"]
OcrQueryType = Literal["all", "person", "corporation", "alliance"]
OcrQueryStatus = Literal["pending", "running", "partial", "completed", "failed", "expired"]
OcrNodeStatus = Literal["pending", "captured", "timeout", "ocr_failed", "unavailable"]


def _copy_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


@dataclass(frozen=True)
class SystemRef:
    name: str
    system_id: int | None = None


@dataclass(frozen=True)
class MonitoringNode:
    client_id: str
    system_name: str
    system_id: int | None = None


@dataclass(frozen=True)
class MonitoringNodeChange:
    change: Literal["online", "offline", "moved"]
    node_id: str
    system_name: str | None = None
    from_system: str | None = None
    to_system: str | None = None


@dataclass
class BootstrapPayload:
    schema_version: str
    generated_at: str
    map: dict[str, Any]
    alerts: list[dict[str, Any]] = field(default_factory=list)
    active_intel: list[dict[str, Any]] = field(default_factory=list)
    hostile_personnel: list[dict[str, Any]] = field(default_factory=list)
    clients: dict[str, Any] = field(default_factory=dict)
    monitoring_nodes: list[dict[str, Any]] = field(default_factory=list)
    monitoring_nodes_version: str = ""
    monitoring_node_changes: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "BootstrapPayload":
        known = {"schema_version", "generated_at", "map", "alerts", "active_intel", "hostile_personnel", "clients", "monitoring_nodes", "monitoring_nodes_version", "monitoring_node_changes"}
        return cls(
            schema_version=str(payload.get("schema_version") or ""),
            generated_at=str(payload.get("generated_at") or ""),
            map=_copy_dict(payload.get("map")),
            alerts=[x for x in payload.get("alerts", []) if isinstance(x, dict)],
            active_intel=[x for x in payload.get("active_intel", []) if isinstance(x, dict)],
            hostile_personnel=[x for x in payload.get("hostile_personnel", []) if isinstance(x, dict)],
            clients=_copy_dict(payload.get("clients")),
            monitoring_nodes=[x for x in payload.get("monitoring_nodes", []) if isinstance(x, dict)],
            monitoring_nodes_version=str(payload.get("monitoring_nodes_version") or ""),
            monitoring_node_changes=[x for x in payload.get("monitoring_node_changes", []) if isinstance(x, dict)],
            extra={k: v for k, v in payload.items() if k not in known},
        )


@dataclass(frozen=True)
class AlertEvent:
    id: str
    system_name: str
    level: Level
    score: int
    created_at: str
    hostile_count: int = 0
    presence_only: bool = False
    source_observation_id: str = ""
    verified_characters: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class SafeEvent:
    system_name: str
    created_at: str
    hostile_count: int = 0
    active: bool = False


@dataclass(frozen=True)
class MonitoringNodeEvent:
    schema_version: str
    generated_at: str
    changes: tuple[dict[str, Any], ...]
    nodes_version: str
    nodes: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class HostileMovementEvent:
    movement_id: str
    occurred_at: str
    from_system: SystemRef
    to_system: SystemRef
    hostile_count: int
    personnel: tuple[dict[str, Any], ...] = ()
    source: Literal["detector", "bootstrap_reconciliation"] = "detector"
    schema_version: str = "hostile_movement_event.v1"


@dataclass
class EsiGatewayHealth:
    ok: bool
    service: str
    version: str
    requests: int = 0
    upstream_requests: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    endpoints: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class OcrName:
    name: str
    entity_id: int | None = None
    confidence: float | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "OcrName":
        return cls(name=str(payload.get("name") or ""), entity_id=payload.get("entity_id"), confidence=payload.get("confidence"))


@dataclass(frozen=True)
class OcrQueryCreateRequest:
    request_id: str
    query_type: OcrQueryType
    target_name: str | None = None
    target_id: int | None = None
    client_id: str | None = None
    source_instance: str | None = None
    window_ids: tuple[str, ...] = ()
    expires_in_seconds: int | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "OcrQueryCreateRequest":
        return cls(
            request_id=str(payload.get("request_id") or ""),
            query_type=payload.get("query_type", "all"),
            target_name=payload.get("target_name"),
            target_id=payload.get("target_id"),
            client_id=payload.get("client_id"),
            source_instance=payload.get("source_instance"),
            window_ids=tuple(x for x in payload.get("window_ids", []) if isinstance(x, str)),
            expires_in_seconds=payload.get("expires_in_seconds"),
        )


@dataclass(frozen=True)
class CaptureOcrOnceCommand:
    request_id: str
    query_type: OcrQueryType
    expires_at: str
    target_name: str | None = None
    target_id: int | None = None
    window_ids: tuple[str, ...] = ()
    command: Literal["capture_ocr_once"] = "capture_ocr_once"

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CaptureOcrOnceCommand":
        return cls(
            request_id=str(payload.get("request_id") or ""),
            query_type=payload.get("query_type", "all"),
            expires_at=str(payload.get("expires_at") or ""),
            target_name=payload.get("target_name"),
            target_id=payload.get("target_id"),
            window_ids=tuple(x for x in payload.get("window_ids", []) if isinstance(x, str)),
        )


@dataclass(frozen=True)
class HeartbeatResponse:
    commands: tuple[CaptureOcrOnceCommand, ...] = ()

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "HeartbeatResponse":
        return cls(commands=tuple(CaptureOcrOnceCommand.from_dict(x) for x in payload.get("commands", []) if isinstance(x, dict)))


@dataclass(frozen=True)
class OcrQueryResult:
    request_id: str
    client_id: str
    window_id: str
    query_type: OcrQueryType
    status: Literal["success", "ocr_failed"]
    captured_at: str
    names: tuple[OcrName, ...] = ()
    source_instance: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "OcrQueryResult":
        return cls(
            request_id=str(payload.get("request_id") or ""),
            client_id=str(payload.get("client_id") or ""),
            window_id=str(payload.get("window_id") or ""),
            query_type=payload.get("query_type", "all"),
            status=payload.get("status", "success"),
            captured_at=str(payload.get("captured_at") or ""),
            names=tuple(OcrName.from_dict(x) for x in payload.get("names", []) if isinstance(x, dict)),
            source_instance=payload.get("source_instance"),
            error_code=payload.get("error_code"),
            error_message=payload.get("error_message"),
        )


@dataclass(frozen=True)
class OcrQueryNode:
    node_id: str
    client_id: str
    window_id: str
    status: OcrNodeStatus
    source_instance: str | None = None
    names: tuple[OcrName, ...] = ()
    error_code: str | None = None
    error_message: str | None = None
    captured_at: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "OcrQueryNode":
        return cls(
            node_id=str(payload.get("node_id") or ""),
            client_id=str(payload.get("client_id") or ""),
            window_id=str(payload.get("window_id") or ""),
            status=payload.get("status", "pending"),
            source_instance=payload.get("source_instance"),
            names=tuple(OcrName.from_dict(x) for x in payload.get("names", []) if isinstance(x, dict)),
            error_code=payload.get("error_code"),
            error_message=payload.get("error_message"),
            captured_at=payload.get("captured_at"),
        )


@dataclass(frozen=True)
class OcrQueryAggregateResult:
    names: tuple[OcrName, ...] = ()
    received_nodes: int = 0
    timed_out_nodes: int = 0
    failed_nodes: int = 0

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "OcrQueryAggregateResult":
        return cls(
            names=tuple(OcrName.from_dict(x) for x in payload.get("names", []) if isinstance(x, dict)),
            received_nodes=int(payload.get("received_nodes", 0)),
            timed_out_nodes=int(payload.get("timed_out_nodes", 0)),
            failed_nodes=int(payload.get("failed_nodes", 0)),
        )


@dataclass(frozen=True)
class OcrQueryTask:
    request_id: str
    query_type: OcrQueryType
    status: OcrQueryStatus
    created_at: str
    expires_at: str
    nodes: tuple[OcrQueryNode, ...] = ()
    task_id: str | None = None
    target_name: str | None = None
    target_id: int | None = None
    result: OcrQueryAggregateResult | None = None
    error_code: str | None = None
    error_message: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "OcrQueryTask":
        raw_result = payload.get("result")
        return cls(
            request_id=str(payload.get("request_id") or ""),
            query_type=payload.get("query_type", "all"),
            status=payload.get("status", "pending"),
            created_at=str(payload.get("created_at") or ""),
            expires_at=str(payload.get("expires_at") or ""),
            nodes=tuple(OcrQueryNode.from_dict(x) for x in payload.get("nodes", []) if isinstance(x, dict)),
            task_id=payload.get("task_id"),
            target_name=payload.get("target_name"),
            target_id=payload.get("target_id"),
            result=OcrQueryAggregateResult.from_dict(raw_result) if isinstance(raw_result, dict) else None,
            error_code=payload.get("error_code"),
            error_message=payload.get("error_message"),
        )
