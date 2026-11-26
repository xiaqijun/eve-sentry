"""Check the container's gateway listener without sending upstream ESI traffic."""

import http.client
import json
import os


def main() -> int:
    host = os.environ.get("EVE_SENTRY_ESI_GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("EVE_SENTRY_ESI_GATEWAY_PORT", "8787"))
    connection = http.client.HTTPConnection(host, port, timeout=2)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        status = json.loads(response.read(65536))
        healthy = (response.status == 200 and status.get("ok") is True
                   and status.get("service") == "eve-sentry-esi-gateway"
                   and status.get("transport_mode") == "relay")
        return 0 if healthy else 1
    except (OSError, ValueError, AttributeError, http.client.HTTPException):
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
