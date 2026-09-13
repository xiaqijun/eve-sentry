"""Conservative response age for public ESI data; timestamps never renew old data."""

import math

from app.esi.contact_http import http_date


def public_freshness(metadata, *, maximum=3600):
    headers = {key.lower(): value for key, value in metadata["headers"].items()}
    received, started = metadata["received"], metadata["started"]
    date, expires = http_date(headers.get("date")), http_date(headers.get("expires"))
    directives = {}
    for part in headers.get("cache-control", "").lower().split(","):
        key, _, value = part.strip().partition("=")
        directives[key] = value.strip('"')
    try:
        age = float(headers.get("age", 0))
        if not math.isfinite(age) or age < 0:
            raise ValueError("invalid age")
        lifetimes = [float(maximum)]
        if "max-age" in directives:
            lifetime = float(directives["max-age"])
            if not math.isfinite(lifetime) or lifetime < 0:
                raise ValueError("invalid lifetime")
            lifetimes.append(lifetime)
        if expires is not None and date is not None:
            lifetimes.append(max(0, expires - date))
        # ESI's official OpenAPI specifies a 3600-second client cache TTL for
        # POST affiliation, whose responses may omit HTTP cache directives.
        # Explicit (even invalid) cache headers must never get this fallback.
        if ("cache-control" not in headers and "expires" not in headers
                and metadata.get("method") == "POST"
                and metadata.get("path", "").rstrip("/") in {
                    "/latest/characters/affiliation", "/characters/affiliation"}):
            lifetimes.append(3600.0)
        if date is None or len(lifetimes) == 1 or "no-cache" in directives or "no-store" in directives:
            return {"fetched_at": received, "expires_at": received, "degraded": True}
        age = max(max(0, received - date), age + max(0, received - started))
        return {"fetched_at": received, "expires_at": received + max(0, min(lifetimes) - age)}
    except (TypeError, ValueError, OverflowError):
        return {"fetched_at": received, "expires_at": received, "degraded": True}
