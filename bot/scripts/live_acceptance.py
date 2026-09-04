from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from anyio import Path as AsyncPath
from redis.asyncio import Redis

import eve_risk
from eve_risk.analysis import FleetAnalyzer
from eve_risk.clients.base import request_with_retries
from eve_risk.clients.esi import ESIClient
from eve_risk.clients.images import EveImageClient
from eve_risk.clients.zkill import ZKillClient, aggregate_character_stats
from eve_risk.config import get_settings
from eve_risk.report import ReportAssets, ReportRenderer
from eve_risk.sde import SDELocalization
from eve_risk.ship_roles import ShipRoleClassifier


async def main(preselected: dict[str, list[int]] | None = None) -> None:
    settings = get_settings()
    settings.require_zkill()
    redis = Redis.from_url(settings.redis_url)
    http = httpx.AsyncClient(
        headers={"Accept": "application/json"},
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
    sde = SDELocalization(settings.sde_index_path)
    classifier = ShipRoleClassifier(
        Path(eve_risk.__file__).parent / "data/ship_role_overrides.json"
    )
    esi = ESIClient(http, settings.esi_base_url, classifier, sde=sde)
    images = EveImageClient(http, settings.eve_image_base_url)
    zkill = ZKillClient(
        http,
        redis,
        settings.zkill_base_url,
        settings.zkill_user_agent,
        settings.zkill_request_interval_seconds,
        settings.zkill_cache_ttl_seconds,
    )
    analyzer = FleetAnalyzer(
        settings.analysis_window_days,
        settings.recent_weight_days,
        settings.friendly_character_id_set,
        settings.friendly_corporation_id_set,
        settings.friendly_alliance_id_set,
    )
    renderer = ReportRenderer(settings.report_width, settings.report_max_height, settings.font_path)
    try:
        if preselected:
            scenarios = preselected
        else:
            recent = await _recent_kills(http, settings.zkill_base_url, settings.zkill_user_agent)
            scenarios = _select_scenarios(recent)
        all_ids = {character_id for ids in scenarios.values() for character_id in ids}
        names = await esi.resolve_entity_names(all_ids)
        fetches: dict[int, list[object]] = {}
        lifetime_stats_by_id = {}
        for character_id in all_ids:
            kills, losses, lifetime_stats = await asyncio.gather(
                zkill.fetch_character(character_id, "kills"),
                zkill.fetch_character(character_id, "losses"),
                zkill.fetch_character_stats(character_id),
            )
            fetches[character_id] = [kills, losses]
            lifetime_stats_by_id[character_id] = lifetime_stats

        results: list[dict[str, object]] = []
        output_dir = AsyncPath("/tmp/eve-risk-live")
        await output_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        for key, character_ids in scenarios.items():
            requested_names = [
                names.get(character_id, f"角色 {character_id}") for character_id in character_ids
            ]
            identities, invalid = await esi.resolve_characters(requested_names)
            identity_ids = {item.character_id for item in identities}
            mails = {
                mail.killmail_id: mail
                for character_id in character_ids
                for fetch in fetches[character_id]
                for mail in fetch.killmails
                if now - timedelta(days=settings.analysis_window_days) <= mail.killmail_time <= now
            }
            killmails = list(mails.values())
            type_ids = {
                participant.ship_type_id
                for mail in killmails
                for participant in mail.participants
                if participant.ship_type_id is not None
            }
            ship_types = await esi.fetch_ship_types(type_ids)
            associate_ids = analyzer.top_associate_ids(killmails, identity_ids)
            associate_names = await esi.resolve_entity_names(associate_ids)
            solar_systems = sde.solar_systems({mail.solar_system_id for mail in killmails})
            report = analyzer.analyze(
                request_id=f"live-{key}",
                requested_count=len(requested_names),
                identities=identities,
                invalid_names=invalid,
                killmails=killmails,
                ship_types=ship_types,
                covered_character_ids=identity_ids,
                associate_names=associate_names,
                solar_systems=solar_systems,
                now=now,
            )
            report.lifetime_stats = aggregate_character_stats(
                [lifetime_stats_by_id[character_id] for character_id in character_ids]
            )
            report.recent_engagements = await zkill.enrich_related_battles(
                report.recent_engagements,
                identity_ids,
            )
            report.latest_engagement = next(
                (
                    item
                    for item in report.recent_engagements
                    if item.destroyed_count > 0
                ),
                report.recent_engagements[0] if report.recent_engagements else None,
            )
            portrait_ids = {
                profile.character_id for profile in report.profiles[:4]
            } | {associate.id for associate in report.common_associates[:5]}
            ship_icon_ids = {
                int(ship.id) for ship in report.top_ships[:5] if ship.id is not None
            } | {ship.id for ship in report.pilot_ships[:5]}
            ship_icon_ids.update(
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
            portraits, ship_icons, corporation_logos, alliance_logos = (
                await images.fetch_report_assets(
                    portrait_ids,
                    ship_icon_ids,
                    corporation_ids,
                    alliance_ids,
                )
            )
            output = output_dir / f"{key}.png"
            await output.write_bytes(
                renderer.render(
                    report,
                    ReportAssets(
                        character_portraits=portraits,
                        ship_icons=ship_icons,
                        corporation_logos=corporation_logos,
                        alliance_logos=alliance_logos,
                    ),
                )
            )
            latest = report.latest_engagement
            results.append(
                {
                    "scenario": key,
                    "characters": requested_names,
                    "events": report.data_events,
                    "engagements": report.engagement_count,
                    "coverage": report.coverage_ratio,
                    "latest": (
                        {
                            "outcome": latest.outcome,
                            "detail": latest.result_detail,
                            "value": latest.total_value,
                            "system": latest.system_name,
                            "fleet_size": latest.fleet_size,
                            "ships": [f"{item.name}×{item.count}" for item in latest.ships[:5]],
                        }
                        if latest
                        else None
                    ),
                    "associates": [
                        {
                            "name": item.name,
                            "id": item.id,
                            "relation": item.relation_label,
                            "engagements": item.engagement_count,
                            "days": item.distinct_days,
                        }
                        for item in report.common_associates[:5]
                    ],
                    "image": str(output),
                }
            )
        print(json.dumps(results, ensure_ascii=False, indent=2))
    finally:
        sde.close()
        await http.aclose()
        await redis.aclose()


async def _recent_kills(http: httpx.AsyncClient, base_url: str, user_agent: str) -> list[dict]:
    headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip"}
    for path in ("kills/pastSeconds/86400/", "kills/iskValue/1000000000/pastSeconds/604800/"):
        response = await request_with_retries(
            http,
            "GET",
            f"{base_url.rstrip('/')}/{path}",
            headers=headers,
            timeout=45,
        )
        payload = response.json()
        if isinstance(payload, list) and payload:
            return payload

    recent: list[dict] = []
    redisq_url = "https://redisq.zkillboard.com/listen.php"
    for _ in range(60):
        response = await request_with_retries(
            http,
            "GET",
            redisq_url,
            params={"queueID": "eve-risk-acceptance-v2", "ttw": 0},
            headers=headers,
            timeout=20,
        )
        package = response.json().get("package")
        if not package:
            await asyncio.sleep(0.2)
            continue
        mail = dict(package.get("killmail") or {})
        mail["zkb"] = package.get("zkb") or {}
        if mail:
            recent.append(mail)
        if len(recent) >= 30:
            return recent
    if not recent:
        raise RuntimeError("zKillboard recent kills returned no usable data")
    return recent


def _select_scenarios(recent: list[dict]) -> dict[str, list[int]]:
    attacker_counts: Counter[int] = Counter()
    victims: list[int] = []
    multi_candidates: list[list[int]] = []
    for mail in recent:
        victim_id = (mail.get("victim") or {}).get("character_id")
        if victim_id:
            victims.append(int(victim_id))
        attackers = list(
            dict.fromkeys(
                int(item["character_id"])
                for item in mail.get("attackers") or []
                if item.get("character_id")
            )
        )
        attacker_counts.update(attackers)
        if len(attackers) >= 3:
            multi_candidates.append(attackers[:3])
    if not attacker_counts or not victims or not multi_candidates:
        raise RuntimeError("Not enough recent public kills to build acceptance scenarios")
    single_id = attacker_counts.most_common(1)[0][0]
    loss_id = next((item for item in victims if item != single_id), victims[0])
    multi_ids = next(
        (items for items in multi_candidates if single_id not in items), multi_candidates[0]
    )
    return {
        "single_frequent_team": [single_id],
        "recent_loss": [loss_id],
        "multi_character_fleet": multi_ids,
    }


def _parse_ids(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-ids", default="")
    parser.add_argument("--single", type=int)
    parser.add_argument("--loss", type=int)
    parser.add_argument("--multi", default="")
    args = parser.parse_args()
    selected: dict[str, list[int]] = {}
    if args.seed_ids:
        selected.update(
            {f"seed_{character_id}": [character_id] for character_id in _parse_ids(args.seed_ids)}
        )
    if args.single:
        selected["single_frequent_team"] = [args.single]
    if args.loss:
        selected["recent_loss"] = [args.loss]
    if args.multi:
        selected["multi_character_fleet"] = _parse_ids(args.multi)
    asyncio.run(main(selected or None))
