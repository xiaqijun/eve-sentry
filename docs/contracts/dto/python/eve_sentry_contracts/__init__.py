"""Wire DTOs for the EVE Sentry integration contracts."""

from .dto import (
    AlertEvent,
    BootstrapPayload,
    CaptureOcrOnceCommand,
    EsiGatewayHealth,
    HeartbeatResponse,
    HostileMovementEvent,
    MonitoringNodeEvent,
    OcrName,
    OcrQueryAggregateResult,
    OcrQueryCreateRequest,
    OcrQueryNode,
    OcrQueryResult,
    OcrQueryTask,
    SafeEvent,
)

__all__ = [
    "AlertEvent",
    "BootstrapPayload",
    "CaptureOcrOnceCommand",
    "EsiGatewayHealth",
    "HeartbeatResponse",
    "HostileMovementEvent",
    "MonitoringNodeEvent",
    "OcrName",
    "OcrQueryAggregateResult",
    "OcrQueryCreateRequest",
    "OcrQueryNode",
    "OcrQueryResult",
    "OcrQueryTask",
    "SafeEvent",
]
