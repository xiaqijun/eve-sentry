from __future__ import annotations

import asyncio
import hashlib
import logging
import struct
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from arq.connections import ArqRedis, RedisSettings

from eve_risk.admission import AdmissionController
from eve_risk.analysis import FleetAnalyzer
from eve_risk.clients.esi import ESIClient
from eve_risk.clients.images import EveImageClient
from eve_risk.clients.qq import QQOpenAPIClient
from eve_risk.clients.zkill import (
    ZKillClient,
    ZKillFetchResult,
    aggregate_character_stats,
)
from eve_risk.config import Settings, get_settings
from eve_risk.domain import (
    AnalysisRequest,
    CharacterIdentity,
    Killmail,
    ShipTypeInfo,
    ZKillStats,
)
from eve_risk.report import ReportAssets, ReportRenderer
from eve_risk.sde import SDELocalization
from eve_risk.ship_roles import ShipRoleClassifier
from eve_risk.storage import Repository, create_session_factory

logger = logging.getLogger(__name__)

REPORT_CACHE_TTL_SECONDS = 600
REPORT_CACHE_MAGIC = b"ERPT1"
REPORT_CACHE_HEADER = struct.Struct("!II")


async def startup(ctx: dict[str, object]) -> None:
    settings = get_settings()
    settings.require_qq()
    settings.require_zkill()
    logging.getLogger("arq.worker").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    redis = ctx["redis"]
    http = httpx.AsyncClient(
        headers={"Accept": "application/json"},
        limits=httpx.Limits(max_connections=30, max_keepalive_connections=15),
    )
    engine, sessions = create_session_factory(settings.database_url)
    classifier = ShipRoleClassifier(Path(__file__).parent / "data" / "ship_role_overrides.json")
    sde = SDELocalization(settings.sde_index_path)
    if not sde.available:
        logger.warning(
            "Official SDE Chinese index is unavailable; ESI Chinese fallback will be used"
        )

    ctx.update(
        settings=settings,
        http=http,
        engine=engine,
        repository=Repository(sessions),
        sde=sde,
        esi=ESIClient(http, settings.esi_base_url, classifier, sde=sde),
        images=EveImageClient(http, settings.eve_image_base_url),
        zkill=ZKillClient(
            http,
            redis,
            settings.zkill_base_url,
            settings.zkill_user_agent,
            settings.zkill_request_interval_seconds,
            settings.zkill_cache_ttl_seconds,
        ),
        qq=QQOpenAPIClient(
            http,
            redis,
            settings.qq_app_id,
            settings.qq_app_secret,
            settings.qq_token_url,
            settings.qq_api_base_url,
        ),
        analyzer=FleetAnalyzer(
            settings.analysis_window_days,
            settings.recent_weight_days,
            settings.friendly_character_id_set,
            settings.friendly_corporation_id_set,
            settings.friendly_alliance_id_set,
        ),
        renderer=ReportRenderer(
            settings.report_width, settings.report_max_height, settings.font_path
        ),
        admission=AdmissionController(
            redis,
            context_ttl=settings.qq_context_ttl_seconds,
            member_ttl=settings.member_rate_limit_seconds,
            group_ttl=settings.group_job_ttl_seconds,
            max_jobs=settings.global_max_jobs,
        ),
    )


async def shutdown(ctx: dict[str, object]) -> None:
    http = ctx.get("http")
    if http:
        await http.aclose()
    engine = ctx.get("engine")
    if engine:
        await engine.dispose()
    sde = ctx.get("sde")
    if sde:
        sde.close()


async def run_analysis_job(ctx: dict[str, object], request_payload: dict[str, object]) -> None:
    request = AnalysisRequest.model_validate(request_payload)
    started_at = time.monotonic()
    settings: Settings = ctx["settings"]
    redis: ArqRedis = ctx["redis"]
    esi: ESIClient = ctx["esi"]
    images: EveImageClient = ctx["images"]
    zkill: ZKillClient = ctx["zkill"]
    qq: QQOpenAPIClient = ctx["qq"]
    analyzer: FleetAnalyzer = ctx["analyzer"]
    renderer: ReportRenderer = ctx["renderer"]
    admission: AdmissionController = ctx["admission"]
    repository: Repository = ctx["repository"]
    resolved_count = 0
    data_events = 0
    persistence_task: asyncio.Task[None] | None = None
    job_start_task = asyncio.create_task(_record_job_start(repository, request))

    try:
        cached_report = await _get_cached_report(redis, request.character_names)
        if cached_report is not None:
            image, resolved_count, data_events = cached_report
            await _send_report_image(qq, request, image)
            await job_start_task
            await _record_job_finish(
                repository,
                request.request_id,
                status="completed",
                resolved_count=resolved_count,
                data_events=data_events,
            )
            logger.info(
                "request_id=%s completed elapsed=%.2fs cache_hit=true",
                request.request_id,
                time.monotonic() - started_at,
            )
            return

        identities, invalid_names = await esi.resolve_characters(request.character_names)
        resolved_count = len(identities)
        if not identities:
            await _send_report_text(
                qq, request, "没有找到任何 Tranquility 角色，请检查名字拼写。"
            )
            await job_start_task
            await _record_job_finish(
                repository,
                request.request_id,
                status="failed",
                resolved_count=0,
                data_events=0,
                error_code="no_valid_characters",
            )
            return

        lifetime_stats_task = asyncio.create_task(_fetch_lifetime_stats(zkill, identities))
        fetch_results, fetch_warnings = await _fetch_with_deadline(
            zkill, identities, request.fetch_deadline_at
        )
        lifetime_stats = await _with_timeout(
            lifetime_stats_task,
            request.fetch_deadline_at,
            default=None,
        )
        covered_ids = {result.character_id for result in fetch_results}
        truncated_ids = {result.character_id for result in fetch_results if result.truncated}
        if truncated_ids:
            fetch_warnings.append(f"{len(truncated_ids)} 个角色达到上游单次返回上限，样本可能截断")

        now = datetime.now(UTC)
        killmails, analysis_window_days = _select_analysis_window(
            _dedupe_killmails(fetch_results),
            now,
            settings.analysis_window_days,
        )
        if analysis_window_days > settings.analysis_window_days:
            fetch_warnings.append(
                f"近 {settings.analysis_window_days} 天没有公开战报，已展示可获取的历史样本"
            )
        input_ids = {identity.character_id for identity in identities}
        type_ids = {
            participant.ship_type_id
            for mail in killmails
            for participant in mail.participants
            if participant.ship_type_id is not None
        }
        associate_ids = analyzer.top_associate_ids(killmails, input_ids)
        ship_types, associate_names = await asyncio.gather(
            _with_timeout(esi.fetch_ship_types(type_ids), request.fetch_deadline_at, default={}),
            _with_timeout(
                esi.resolve_entity_names(associate_ids),
                request.fetch_deadline_at,
                default={},
            ),
        )
        solar_systems = ctx["sde"].solar_systems({mail.solar_system_id for mail in killmails})

        report = analyzer.analyze(
            request_id=request.request_id,
            requested_count=len(request.character_names),
            identities=identities,
            invalid_names=invalid_names,
            killmails=killmails,
            ship_types=ship_types,
            covered_character_ids=covered_ids,
            associate_names=associate_names,
            solar_systems=solar_systems,
            warnings=fetch_warnings,
            window_days=analysis_window_days,
        )
        if isinstance(lifetime_stats, ZKillStats):
            report.lifetime_stats = lifetime_stats

        portrait_ids = {profile.character_id for profile in report.profiles[:4]} | {
            associate.id for associate in report.common_associates[:5]
        }
        initial_ship_icon_ids = {
            int(ship.id) for ship in report.top_ships[:5] if ship.id is not None
        } | {ship.id for ship in report.pilot_ships[:5]}
        initial_ship_icon_ids.update(
            int(ship.id)
            for engagement in report.recent_engagements[:5]
            for ship in engagement.destroyed_ships + engagement.lost_ships
            if ship.id is not None
        )
        corporation_ids = {
            profile.corporation_id
            for profile in report.profiles[:4]
            if profile.corporation_id is not None
        }
        alliance_ids = {
            profile.alliance_id
            for profile in report.profiles[:4]
            if profile.alliance_id is not None
        }
        assets_task = asyncio.create_task(
            images.fetch_report_assets(
                portrait_ids,
                initial_ship_icon_ids,
                corporation_ids,
                alliance_ids,
            )
        )
        persistence_task = asyncio.create_task(
            _persist_analysis_data(
                repository,
                request,
                identities,
                ship_types,
                killmails,
                report.generated_at,
                fetch_results,
            )
        )

        related_engagements = await _with_timeout(
            zkill.enrich_related_battles(report.recent_engagements, input_ids),
            request.reply_deadline_at,
            default=report.recent_engagements,
        )
        if isinstance(related_engagements, list):
            report.recent_engagements = related_engagements
            report.latest_engagement = next(
                (item for item in related_engagements if item.destroyed_count > 0),
                related_engagements[0] if related_engagements else None,
            )
        data_events = report.data_events

        final_ship_icon_ids = set(initial_ship_icon_ids)
        final_ship_icon_ids.update(
            int(ship.id)
            for engagement in report.recent_engagements[:5]
            for ship in engagement.destroyed_ships + engagement.lost_ships
            if ship.id is not None
        )
        portrait_images, ship_icons, corporation_logos, alliance_logos = await _with_timeout(
            assets_task,
            request.reply_deadline_at,
            default=({}, {}, {}, {}),
        )
        extra_ship_icons = await _with_timeout(
            images.fetch_ship_icons(final_ship_icon_ids - initial_ship_icon_ids),
            request.reply_deadline_at,
            default={},
        )
        if isinstance(ship_icons, dict) and isinstance(extra_ship_icons, dict):
            ship_icons.update(extra_ship_icons)
        image = renderer.render(
            report,
            ReportAssets(
                character_portraits=portrait_images,
                ship_icons=ship_icons,
                corporation_logos=corporation_logos,
                alliance_logos=alliance_logos,
            ),
        )
        remaining = max(1.0, (request.reply_deadline_at - datetime.now(UTC)).total_seconds())
        async with asyncio.timeout(remaining):
            try:
                await _send_report_image(qq, request, image)
                await _set_cached_report(
                    redis,
                    request.character_names,
                    image,
                    resolved_count,
                    data_events,
                )
            except Exception:
                logger.exception("request_id=%s image_send_failed", request.request_id)
                await qq.send_text(
                    request.group_openid,
                    request.msg_id,
                    "报告图片发送失败，请稍后重试。",
                    msg_seq=1,
                )
        await job_start_task
        await _record_job_finish(
            repository,
            request.request_id,
            status="completed",
            resolved_count=resolved_count,
            data_events=data_events,
        )
        logger.info(
            "request_id=%s completed elapsed=%.2fs cache_hit=false",
            request.request_id,
            time.monotonic() - started_at,
        )
    except TimeoutError:
        logger.warning("request_id=%s reply_deadline_exceeded", request.request_id)
        await job_start_task
        await _record_job_finish(
            repository,
            request.request_id,
            status="failed",
            resolved_count=resolved_count,
            data_events=data_events,
            error_code="reply_deadline",
        )
        await _safe_error_reply(qq, request, "分析超过 QQ 回复时限，请稍后缩小名单重试。")
    except Exception:
        logger.exception("request_id=%s analysis_failed", request.request_id)
        await job_start_task
        await _record_job_finish(
            repository,
            request.request_id,
            status="failed",
            resolved_count=resolved_count,
            data_events=data_events,
            error_code="analysis_failed",
        )
        await _safe_error_reply(qq, request, "分析失败，上游服务可能暂时不可用，请稍后重试。")
    finally:
        if persistence_task is not None:
            await persistence_task
        await admission.release(request.request_id, request.group_openid)


async def _fetch_with_deadline(
    zkill: ZKillClient, identities: list[object], deadline: datetime
) -> tuple[list[ZKillFetchResult], list[str]]:
    tasks = [
        asyncio.create_task(zkill.fetch_character(identity.character_id, direction))
        for identity in identities
        for direction in ("kills", "losses")
    ]
    timeout = max(0.1, (deadline - datetime.now(UTC)).total_seconds())
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    results: list[ZKillFetchResult] = []
    failures = 0
    for task in done:
        try:
            results.append(task.result())
        except Exception:
            failures += 1
    warnings = [f"{failures} 个战报方向抓取失败"] if failures else []
    if pending:
        warnings.append(f"达到抓取截止时间，跳过 {len(pending)} 个未完成请求")
    return results, warnings


async def _fetch_lifetime_stats(
    zkill: ZKillClient,
    identities: list[CharacterIdentity],
) -> ZKillStats | None:
    results = await asyncio.gather(
        *(zkill.fetch_character_stats(identity.character_id) for identity in identities),
        return_exceptions=True,
    )
    return aggregate_character_stats([item for item in results if isinstance(item, ZKillStats)])


async def _with_timeout(awaitable: object, deadline: datetime, default: object) -> object:
    remaining = max(0.1, (deadline - datetime.now(UTC)).total_seconds())
    try:
        return await asyncio.wait_for(awaitable, timeout=remaining)
    except Exception:
        return default


def _dedupe_killmails(results: list[ZKillFetchResult]) -> list[Killmail]:
    return list(
        {mail.killmail_id: mail for result in results for mail in result.killmails}.values()
    )


def _filter_window(killmails: list[Killmail], now: datetime, window_days: int) -> list[Killmail]:
    start = now - timedelta(days=window_days)
    future_tolerance = now + timedelta(minutes=5)
    return [mail for mail in killmails if start <= mail.killmail_time <= future_tolerance]


def _select_analysis_window(
    killmails: list[Killmail], now: datetime, default_window_days: int
) -> tuple[list[Killmail], int]:
    recent = _filter_window(killmails, now, default_window_days)
    if recent or not killmails:
        return recent, default_window_days

    future_tolerance = now + timedelta(minutes=5)
    historical = [mail for mail in killmails if mail.killmail_time <= future_tolerance]
    if not historical:
        return [], default_window_days
    oldest = min(mail.killmail_time for mail in historical)
    historical_window_days = max(default_window_days, (now - oldest).days + 1)
    return historical, historical_window_days


async def _safe_error_reply(qq: QQOpenAPIClient, request: AnalysisRequest, content: str) -> None:
    if datetime.now(UTC) >= request.reply_deadline_at:
        return
    try:
        await _send_report_text(qq, request, content)
    except Exception:
        logger.exception("request_id=%s error_reply_failed", request.request_id)


async def _send_report_image(
    qq: QQOpenAPIClient, request: AnalysisRequest, image: bytes
) -> None:
    if request.proactive:
        await qq.send_proactive_image(request.group_openid, image)
    else:
        await qq.send_image(request.group_openid, request.msg_id, image, msg_seq=1)


async def _send_report_text(
    qq: QQOpenAPIClient, request: AnalysisRequest, content: str
) -> None:
    if request.proactive:
        await qq.send_proactive_text(request.group_openid, content)
    else:
        await qq.send_text(request.group_openid, request.msg_id, content, msg_seq=1)


def _report_cache_key(character_names: list[str]) -> str:
    normalized = "\0".join(name.strip().casefold() for name in character_names)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    # Bump the namespace when report identity semantics change so an image
    # generated from a stale/mis-resolved character cannot be reused.
    return f"report:image:v3:{digest}"


async def _get_cached_report(
    redis: ArqRedis, character_names: list[str]
) -> tuple[bytes, int, int] | None:
    try:
        cached = await redis.get(_report_cache_key(character_names))
    except Exception:
        logger.warning("report_cache_read_failed", exc_info=True)
        return None
    if not cached:
        return None
    payload = cached if isinstance(cached, bytes) else str(cached).encode("utf-8")
    prefix_size = len(REPORT_CACHE_MAGIC) + REPORT_CACHE_HEADER.size
    if len(payload) <= prefix_size or not payload.startswith(REPORT_CACHE_MAGIC):
        return None
    resolved_count, data_events = REPORT_CACHE_HEADER.unpack_from(payload, len(REPORT_CACHE_MAGIC))
    return payload[prefix_size:], resolved_count, data_events


async def _set_cached_report(
    redis: ArqRedis,
    character_names: list[str],
    image: bytes,
    resolved_count: int,
    data_events: int,
) -> None:
    payload = REPORT_CACHE_MAGIC + REPORT_CACHE_HEADER.pack(resolved_count, data_events) + image
    try:
        await redis.set(
            _report_cache_key(character_names),
            payload,
            ex=REPORT_CACHE_TTL_SECONDS,
        )
    except Exception:
        logger.warning("report_cache_write_failed", exc_info=True)


async def _persist_analysis_data(
    repository: Repository,
    request: AnalysisRequest,
    identities: list[CharacterIdentity],
    ship_types: dict[int, ShipTypeInfo],
    killmails: list[Killmail],
    generated_at: datetime,
    fetch_results: list[ZKillFetchResult],
) -> None:
    try:
        await asyncio.wait_for(
            repository.save_analysis_data(
                identities,
                ship_types,
                killmails,
                generated_at,
                [
                    (
                        result.character_id,
                        result.direction,
                        len(result.killmails),
                        result.truncated,
                    )
                    for result in fetch_results
                ],
            ),
            timeout=15,
        )
    except Exception:
        logger.exception("request_id=%s persistence_failed", request.request_id)


async def _record_job_start(repository: Repository, request: AnalysisRequest) -> None:
    try:
        await asyncio.wait_for(
            repository.record_job_started(
                request.request_id, len(request.character_names), request.received_at
            ),
            timeout=3,
        )
    except Exception:
        logger.warning("request_id=%s job_start_persistence_failed", request.request_id)


async def _record_job_finish(
    repository: Repository,
    request_id: str,
    *,
    status: str,
    resolved_count: int,
    data_events: int,
    error_code: str | None = None,
) -> None:
    try:
        await asyncio.wait_for(
            repository.record_job_finished(
                request_id,
                status=status,
                resolved_count=resolved_count,
                data_events=data_events,
                completed_at=datetime.now(UTC),
                error_code=error_code,
            ),
            timeout=3,
        )
    except Exception:
        logger.warning("request_id=%s job_finish_persistence_failed", request_id)


class WorkerSettings:
    functions = [run_analysis_job]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 3
    job_timeout = 300
    keep_result = 0
    log_results = False
    max_tries = 1
