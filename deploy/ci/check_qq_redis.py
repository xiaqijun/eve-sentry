"""Fail deployment before starting an enabled bot with unusable Redis access."""

from pathlib import Path

from dotenv import dotenv_values
from redis import Redis


if __name__ == "__main__":
    values = dotenv_values(Path("/etc/eve-sentry/eve-sentry.env"), interpolate=False)
    enabled = (values.get("EVE_SENTRY_SERVER_QQ_BOT_ENABLED") or "").lower()
    if enabled not in {"1", "true", "yes", "on"}:
        print("Embedded QQ bot is disabled; Redis preflight skipped")
        raise SystemExit(0)
    url = values.get("EVE_SENTRY_SERVER_QQ_BOT_REDIS_URL")
    if not url:
        raise SystemExit("Enabled QQ bot is missing its Redis URL")
    try:
        with Redis.from_url(url, socket_timeout=5, socket_connect_timeout=5) as client:
            client.ping()
            client.scard("qq:eve-sentry:alert-groups")
            client.get("qq:eve-server:status-state")
    except Exception as error:
        raise SystemExit(f"QQ Redis preflight failed ({type(error).__name__}); run the protected QQ Redis repair workflow") from None
    print("QQ Redis authentication and subscription reads verified")
