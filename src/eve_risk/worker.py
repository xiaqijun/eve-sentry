from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from arq.connections import RedisSettings

from eve_risk.admission import AdmissionController
from eve_risk.analysis import FleetAnalyzer
from eve_risk.clients.esi import ESIClient
from eve_risk.clients.qq import QQOpenAPIClient
from eve_risk.clients.zkill import ZKillClient, ZKillFetchResult
from eve_risk.config import Settings, get_settings
from eve_risk.domain import AnalysisRequest, Killmail
from eve_risk.report import ReportRenderer, build_summary
from eve_risk.sde import SDELocalization
from eve_risk.ship_roles import ShipRoleClassifier
from eve_risk.storage import Repository, create_session_factory

logger = logging.getLogger(__name__)


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
    settings: Settings = ctx["settings"]
    esi: ESIClient = ctx["esi"]
    zkill: ZKillClient = ctx["zkill"]
    qq: QQOpenAPIClient = ctx["qq"]
    analyzer: FleetAnalyzer = ctx["analyzer"]
    renderer: ReportRenderer = ctx["renderer"]
    admission: AdmissionController = ctx["admission"]
    repository: Repository = ctx["repository"]
    resolved_count = 0
    data_events = 0

    try:
        await _record_job_start(repository, request)
        identities, invalid_names = await esi.resolve_characters(request.character_names)
        resolved_count = len(identities)
        if not identities:
            await qq.send_text(
                request.group_openid,
                request.msg_id,
                "没有找到任何 Tranquility 角色，请检查名字拼写。",
                2,
            )
            await _record_job_finish(
                repository,
                request.request_id,
                status="failed",
                resolved_count=0,
                data_events=0,
                error_code="no_valid_characters",
            )
            return

        fetch_results, fetch_warnings = await _fetch_with_deadline(
            zkill, identities, request.fetch_deadline_at
        )
        covered_ids = {result.character_id for result in fetch_results}
        truncated_ids = {result.character_id for result in fetch_results if result.truncated}
        if truncated_ids:
            fetch_warnings.append(f"{len(truncated_ids)} 个角色达到上游单次返回上限，样本可能截断")

        killmails = _filter_window(
            _dedupe_killmails(fetch_results),
            datetime.now(UTC),
            settings.analysis_window_days,
        )
        input_ids = {identity.character_id for identity in identities}
        type_ids = {
            participant.ship_type_id
            for mail in killmails
            for participant in mail.participants
            if participant.ship_type_id is not None
        }
        ship_types = await _with_timeout(
            esi.fetch_ship_types(type_ids), request.fetch_deadline_at, default={}
        )
        associate_ids = analyzer.top_associate_ids(killmails, input_ids)
        associate_names = await _with_timeout(
            esi.resolve_entity_names(associate_ids), request.fetch_deadline_at, default={}
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
        )
        data_events = report.data_events

        try:
            await asyncio.wait_for(
                repository.save_analysis_data(
                    identities,
                    ship_types,
                    killmails,
                    report.generated_at,
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

        image = renderer.render(report)
        remaining = max(1.0, (request.reply_deadline_at - datetime.now(UTC)).total_seconds())
        async with asyncio.timeout(remaining):
            await qq.send_text(
                request.group_openid, request.msg_id, build_summary(report), msg_seq=2
            )
            try:
                await qq.send_image(request.group_openid, request.msg_id, image, msg_seq=3)
            except Exception:
                logger.exception("request_id=%s image_send_failed", request.request_id)
                await qq.send_text(
                    request.group_openid,
                    request.msg_id,
                    "报告图片发送失败，以上文字摘要仍然有效。",
                    msg_seq=3,
                )
        await _record_job_finish(
            repository,
            request.request_id,
            status="completed",
            resolved_count=resolved_count,
            data_events=data_events,
        )
    except TimeoutError:
        logger.warning("request_id=%s reply_deadline_exceeded", request.request_id)
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


async def _safe_error_reply(qq: QQOpenAPIClient, request: AnalysisRequest, content: str) -> None:
    if datetime.now(UTC) >= request.reply_deadline_at:
        return
    try:
        await qq.send_text(request.group_openid, request.msg_id, content, msg_seq=2)
    except Exception:
        logger.exception("request_id=%s error_reply_failed", request.request_id)


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
