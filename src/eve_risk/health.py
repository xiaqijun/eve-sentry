from __future__ import annotations

import time

import uvicorn
from anyio import Path
from fastapi import FastAPI, Response, status
from redis.asyncio import Redis
from sqlalchemy import text

from eve_risk.config import get_settings
from eve_risk.storage import create_session_factory

STARTED_AT = time.time()
app = FastAPI(title="EVE Risk Analysis Health", docs_url=None, redoc_url=None)


@app.get("/health/live")
async def live() -> dict[str, object]:
    return {"status": "ok", "uptime_seconds": int(time.time() - STARTED_AT)}


@app.get("/health/ready")
async def ready(response: Response) -> dict[str, object]:
    settings = get_settings()
    checks = {
        "redis": False,
        "database": False,
        "sde": await Path(settings.sde_index_path).is_file(),
    }
    redis = Redis.from_url(settings.redis_url)
    engine, sessions = create_session_factory(settings.database_url)
    try:
        checks["redis"] = bool(await redis.ping())
        async with sessions() as session:
            await session.execute(text("SELECT 1"))
            checks["database"] = True
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    finally:
        await redis.aclose()
        await engine.dispose()
    return {"status": "ok" if all(checks.values()) else "degraded", "checks": checks}


@app.get("/metrics", response_class=Response)
async def metrics() -> Response:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    try:
        active = await redis.zcard("analysis:active")
    except Exception:
        active = -1
    finally:
        await redis.aclose()
    body = (
        "# HELP eve_risk_active_jobs Active or admitted analysis jobs.\n"
        "# TYPE eve_risk_active_jobs gauge\n"
        f"eve_risk_active_jobs {active}\n"
        "# HELP eve_risk_uptime_seconds Service uptime.\n"
        "# TYPE eve_risk_uptime_seconds gauge\n"
        f"eve_risk_uptime_seconds {int(time.time() - STARTED_AT)}\n"
    )
    return Response(content=body, media_type="text/plain; version=0.0.4")


def main() -> None:
    settings = get_settings()
    uvicorn.run(app, host=settings.health_host, port=settings.health_port)
