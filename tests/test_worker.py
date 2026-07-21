from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from eve_risk.domain import AnalysisRequest, Killmail
from eve_risk.worker import (
    REPORT_CACHE_TTL_SECONDS,
    _get_cached_report,
    _report_cache_key,
    _select_analysis_window,
    _set_cached_report,
    run_analysis_job,
)


@pytest.mark.asyncio
async def test_rendered_report_cache_reuses_normalized_roster() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    try:
        await _set_cached_report(redis, [" MP5K "], b"png-data", 1, 241)

        cached = await _get_cached_report(redis, ["mp5k"])

        assert cached == (b"png-data", 1, 241)
        assert await redis.ttl(_report_cache_key(["MP5K"])) <= REPORT_CACHE_TTL_SECONDS
        assert await _get_cached_report(redis, ["Another Pilot"]) is None
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_cached_query_replies_with_one_image_only() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    qq = SimpleNamespace(send_image=AsyncMock(), send_text=AsyncMock())
    admission = SimpleNamespace(release=AsyncMock())
    repository = SimpleNamespace(
        record_job_started=AsyncMock(),
        record_job_finished=AsyncMock(),
    )
    now = datetime.now(UTC)
    request = AnalysisRequest(
        request_id="cached-request",
        msg_id="source-message",
        group_openid="group-1",
        member_openid="member-1",
        character_names=["MP5K"],
        received_at=now,
        fetch_deadline_at=now + timedelta(minutes=4),
        reply_deadline_at=now + timedelta(minutes=4, seconds=30),
    )
    await _set_cached_report(redis, request.character_names, b"png-data", 1, 241)
    ctx = {
        "settings": SimpleNamespace(),
        "redis": redis,
        "esi": SimpleNamespace(),
        "images": SimpleNamespace(),
        "zkill": SimpleNamespace(),
        "qq": qq,
        "analyzer": SimpleNamespace(),
        "renderer": SimpleNamespace(),
        "admission": admission,
        "repository": repository,
    }

    try:
        await run_analysis_job(ctx, request.model_dump())

        qq.send_image.assert_awaited_once_with("group-1", "source-message", b"png-data", msg_seq=1)
        qq.send_text.assert_not_awaited()
        admission.release.assert_awaited_once_with("cached-request", "group-1")
    finally:
        await redis.aclose()


def test_analysis_window_falls_back_to_historical_killmails() -> None:
    now = datetime(2026, 7, 21, tzinfo=UTC)
    historical = Killmail(
        killmail_id=1,
        killmail_time=now - timedelta(days=400),
        solar_system_id=30000142,
    )

    selected, window_days = _select_analysis_window([historical], now, 90)

    assert selected == [historical]
    assert window_days == 401
