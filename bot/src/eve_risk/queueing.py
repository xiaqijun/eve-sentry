from __future__ import annotations

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from eve_risk.domain import AnalysisRequest


class AnalysisQueue:
    def __init__(self, redis_url: str) -> None:
        self.redis_url = redis_url
        self._pool: ArqRedis | None = None

    async def enqueue(self, request: AnalysisRequest) -> None:
        if self._pool is None:
            self._pool = await create_pool(RedisSettings.from_dsn(self.redis_url))
        job = await self._pool.enqueue_job(
            "run_analysis_job",
            request.model_dump(mode="json"),
            _job_id=request.request_id,
            _expires=330,
        )
        if job is None:
            raise RuntimeError("Analysis job already exists")

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
