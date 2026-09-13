"""Admin-only bounded relay probes; no proxy inheritance or ESI credentials."""

import http.client
import json
import time

from app.esi.diagnostics import failure_summary
from app.esi.transport import TransportEsiClient


def transport_gateway(client):
    """Return None for legacy clients; direct transport has no relay to probe."""
    if not isinstance(client, TransportEsiClient):
        return None
    pool = client.connections
    result = {
        "transport_mode": "relay" if pool.relay_host else "direct",
        "configured": bool(pool.relay_host),
        "reachable": False,
    }
    if not pool.relay_host:
        return result
    host = f"[{pool.relay_host}]" if ":" in pool.relay_host else pool.relay_host
    result["url"] = f"http://{host}:{pool.relay_port}"
    connection = http.client.HTTPConnection(pool.relay_host, pool.relay_port, timeout=2)
    deadline = time.monotonic() + 2
    try:
        # Separate health connection, not an ESI request/permit or CONNECT tunnel.
        connection.request("GET", "/health")
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError("relay health status")
        chunks, size = [], 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("relay health deadline")
            if connection.sock:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(8192, 65537 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > 65536:
                raise ValueError("relay health too large")
        payload = json.loads(b"".join(chunks))
        if (
            not isinstance(payload, dict)
            or payload.get("ok") is not True
            or payload.get("service") != "eve-sentry-esi-gateway"
            or payload.get("transport_mode") not in ("relay", "dual")
        ):
            raise ValueError("relay health unavailable")
        relay = payload.get("relay")
        if not isinstance(relay, dict):
            raise TypeError("relay counters unavailable")
        counts = {
            key: value
            for key, value in relay.items()
            if key
            in {
                "requests",
                "attempts",
                "active",
                "connected",
                "connect_errors",
                "stream_errors",
            }
            and type(value) is int
            and value >= 0
        }
        result.update(
            reachable=True,
            health={
                "ok": True,
                "service": payload["service"],
                "transport_mode": payload["transport_mode"],
                "relay": counts,
            },
        )
        uptime = payload.get("uptime_seconds")
        if type(uptime) in {int, float} and 0 <= uptime < float("inf"):
            result["health"]["uptime_seconds"] = uptime
    except (OSError, ValueError, TypeError, http.client.HTTPException) as exc:
        result["error"] = "Relay 健康检查失败；" + failure_summary(exc)
    finally:
        connection.close()
    return result
