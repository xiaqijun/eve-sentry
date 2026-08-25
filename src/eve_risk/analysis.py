from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from statistics import median
from zoneinfo import ZoneInfo

from eve_risk.doctrines import identify_doctrines
from eve_risk.domain import (
    AnalysisReport,
    AssociateCandidate,
    CharacterIdentity,
    CharacterProfile,
    CompositionMetric,
    Confidence,
    DoctrineMatch,
    EngagementPattern,
    FleetCompositionItem,
    FleetSizeBucket,
    FleetSizeWindow,
    Killmail,
    LatestEngagement,
    NamedMetric,
    Participant,
    PilotShipMetric,
    RelatedBattleRef,
    ShipRole,
    ShipTypeInfo,
    SolarSystemInfo,
    ThreatComponent,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class FleetEngagementSample:
    system_id: int
    started_at: datetime
    last_seen: datetime
    ships_by_character: dict[int, int]
    affiliations_by_character: dict[int, tuple[int | None, int | None]]
    event_count: int
    repeated_character_ids: frozenset[int] = frozenset()
    composition_confidence: Confidence = Confidence.LOW

    @property
    def ship_counts(self) -> Counter[int]:
        return Counter(self.ships_by_character.values())


class FleetAnalyzer:
    def __init__(
        self,
        window_days: int = 90,
        recent_days: int = 30,
        friendly_character_ids: set[int] | None = None,
        friendly_corporation_ids: set[int] | None = None,
        friendly_alliance_ids: set[int] | None = None,
    ) -> None:
        self.window_days = window_days
        self.recent_days = recent_days
        self.friendly_character_ids = frozenset(friendly_character_ids or set())
        self.friendly_corporation_ids = frozenset(friendly_corporation_ids or set())
        self.friendly_alliance_ids = frozenset(friendly_alliance_ids or set())

    def analyze(
        self,
        *,
        request_id: str,
        requested_count: int,
        identities: list[CharacterIdentity],
        invalid_names: list[str],
        killmails: list[Killmail],
        ship_types: dict[int, ShipTypeInfo],
        covered_character_ids: set[int],
        associate_names: dict[int, str] | None = None,
        solar_systems: dict[int, SolarSystemInfo] | None = None,
        warnings: list[str] | None = None,
        window_days: int | None = None,
        now: datetime | None = None,
    ) -> AnalysisReport:
        now = _aware(now or datetime.now(UTC))
        effective_window_days = max(1, window_days or self.window_days)
        window_start = now - timedelta(days=effective_window_days)
        recent_start = now - timedelta(days=self.recent_days)
        seven_day_start = now - timedelta(days=7)
        associate_names = associate_names or {}
        solar_systems = solar_systems or {}
        warnings = list(warnings or [])

        identity_by_id = {identity.character_id: identity for identity in identities}
        input_ids = set(identity_by_id)
        deduped = {
            mail.killmail_id: mail
            for mail in killmails
            if window_start <= _aware(mail.killmail_time) <= now + timedelta(minutes=5)
        }
        mails = sorted(deduped.values(), key=lambda item: item.killmail_time)

        character_stats: dict[int, dict[str, object]] = {
            character_id: {
                "events": 0,
                "weighted": 0.0,
                "last": None,
                "ships": Counter(),
                "roles": Counter(),
                "degree": 0,
                "kills": 0,
                "losses": 0,
                "final_blows": 0,
                "hours": [0.0] * 24,
            }
            for character_id in input_ids
        }
        activity_hours = [0.0] * 24
        activity_week_hours = [[0.0] * 24 for _ in range(7)]
        offensive_events = 0
        loss_events = 0
        recent_7d_kills = 0
        recent_7d_losses = 0
        destroyed_value_30d = 0.0
        lost_value_30d = 0.0
        isk_value_events_30d = 0
        system_counts: Counter[int] = Counter()

        for mail in mails:
            event_time = _aware(mail.killmail_time)
            event_weight = 2.0 if event_time >= recent_start else 1.0
            local_time = event_time.astimezone(SHANGHAI)
            input_participants = [
                participant
                for participant in mail.participants
                if participant.character_id in input_ids
            ]
            if not input_participants:
                continue

            activity_hours[local_time.hour] += event_weight
            activity_week_hours[local_time.weekday()][local_time.hour] += event_weight
            system_counts[mail.solar_system_id] += event_weight
            input_attackers = sorted(
                {
                    participant.character_id
                    for participant in input_participants
                    if not participant.is_victim and participant.character_id is not None
                }
            )
            input_victims = {
                participant.character_id
                for participant in input_participants
                if participant.is_victim and participant.character_id is not None
            }

            if input_attackers:
                offensive_events += 1
                if event_time >= seven_day_start:
                    recent_7d_kills += 1
            if input_victims:
                loss_events += 1
                if event_time >= seven_day_start:
                    recent_7d_losses += 1

            if (
                event_time >= recent_start
                and mail.total_value is not None
                and (input_attackers or input_victims)
            ):
                isk_value_events_30d += 1
                if input_attackers:
                    destroyed_value_30d += mail.total_value
                if input_victims:
                    lost_value_30d += mail.total_value

            for participant in input_participants:
                character_id = participant.character_id
                if character_id is None:
                    continue
                stats = character_stats[character_id]
                stats["events"] = int(stats["events"]) + 1
                stats["weighted"] = float(stats["weighted"]) + event_weight
                stats["hours"][local_time.hour] += event_weight
                if participant.is_victim:
                    stats["losses"] = int(stats["losses"]) + 1
                else:
                    stats["kills"] = int(stats["kills"]) + 1
                if participant.final_blow:
                    stats["final_blows"] = int(stats["final_blows"]) + 1
                last = stats["last"]
                if last is None or event_time > last:
                    stats["last"] = event_time

                if participant.ship_type_id is None:
                    continue
                ship_info = ship_types.get(participant.ship_type_id)
                role = ship_info.role if ship_info else ShipRole.OTHER
                stats["ships"][participant.ship_type_id] += event_weight
                stats["roles"][role.value] += event_weight

        engagements = _build_fleet_engagements(
            mails,
            input_ids,
            self.friendly_character_ids,
            self.friendly_corporation_ids,
            self.friendly_alliance_ids,
        )
        for engagement in engagements:
            pilots = set(engagement.ships_by_character)
            for character_id in pilots & input_ids:
                if pilots - {character_id}:
                    character_stats[character_id]["degree"] = (
                        int(character_stats[character_id]["degree"]) + 1
                    )

        profiles = self._build_profiles(character_stats, identity_by_id, ship_types, recent_start)
        affiliations = self._affiliations(identities)
        engagement_weights = [
            2
            if item.last_seen >= recent_start
            and item.composition_confidence != Confidence.LOW
            else 1
            for item in engagements
        ]
        role_distribution = _role_composition_metrics(engagements, engagement_weights, ship_types)
        top_ships = _ship_composition_metrics(engagements, engagement_weights, ship_types, limit=10)
        doctrine_samples = [
            (item, weight)
            for item, weight in zip(engagements, engagement_weights, strict=True)
            if item.composition_confidence != Confidence.LOW
        ]
        doctrines = identify_doctrines(
            [item.ship_counts for item, _ in doctrine_samples],
            ship_types,
            weights=[weight for _, weight in doctrine_samples],
        )
        fleet_role_counts, fleet_ship_counts = _aggregate_engagement_counts(
            engagements, engagement_weights, ship_types
        )
        common_associates = _associate_candidates(
            engagements,
            input_ids,
            identities,
            associate_names,
            recent_start,
        )
        associate_scores = _associate_event_scores(mails, input_ids)
        common_associates = sorted(
            [
                item.model_copy(
                    update={"score": associate_scores.get(item.id, 0.0)}
                )
                for item in common_associates
            ],
            key=lambda item: (
                item.score,
                item.engagement_count,
                item.distinct_days,
                item.last_seen,
            ),
            reverse=True,
        )
        pilot_ships = _pilot_ship_metrics(mails, input_ids, ship_types)
        core_members = [
            NamedMetric(
                id=profile.character_id, name=profile.name, value=profile.cooccurrence_score
            )
            for profile in sorted(
                profiles,
                key=lambda item: (
                    item.cooccurrence_score,
                    item.weighted_event_count,
                    item.last_activity or datetime.min.replace(tzinfo=UTC),
                ),
                reverse=True,
            )[:8]
            if profile.event_count
        ]

        coverage = len(covered_character_ids & input_ids) / len(input_ids) if input_ids else 0.0
        if coverage < 1:
            warnings.append(
                f"仅成功获取 {len(covered_character_ids & input_ids)}/{len(input_ids)} 个角色的战报数据"
            )
        if not mails:
            warnings.append("近 90 天没有可用公开战报，无法可靠判断舰队习惯")

        kill_efficiency = (
            offensive_events / (offensive_events + loss_events)
            if offensive_events + loss_events
            else None
        )
        isk_efficiency_30d = (
            destroyed_value_30d / (destroyed_value_30d + lost_value_30d)
            if destroyed_value_30d + lost_value_30d > 0
            else None
        )
        weighted_gang_sizes = _weighted_values(
            [len(item.ships_by_character) for item in engagements], engagement_weights
        )
        median_gang = median(weighted_gang_sizes) if weighted_gang_sizes else None
        p75_gang = _percentile(weighted_gang_sizes, 0.75) if weighted_gang_sizes else None
        weighted_solo = sum(
            weight
            for item, weight in zip(engagements, engagement_weights, strict=True)
            if len(item.ships_by_character) == 1
        )
        total_engagement_weight = sum(engagement_weights)
        peak_activity = _peak_activity(activity_hours)
        fleet_size_label = _fleet_size_label(median_gang, p75_gang)
        fleet_size_windows = _fleet_size_windows(
            mails,
            input_ids,
            self.friendly_character_ids,
            self.friendly_corporation_ids,
            self.friendly_alliance_ids,
            now,
        )
        threat_score, threat_level, threat_components, threat_reasons = _threat_rating(
            event_count=len(mails),
            recent_kills=recent_7d_kills,
            recent_losses=recent_7d_losses,
            kill_efficiency=kill_efficiency,
            isk_efficiency_30d=isk_efficiency_30d,
            isk_value_events_30d=isk_value_events_30d,
            p75_gang_size=p75_gang,
            fleet_roles=fleet_role_counts,
            doctrines=doctrines,
            ship_counts=fleet_ship_counts,
            ship_types=ship_types,
            peak_activity=peak_activity,
        )
        top_systems, top_regions = _location_metrics(system_counts, solar_systems)
        recent_engagements = _recent_battles(
            mails,
            input_ids,
            ship_types,
            solar_systems,
            self.friendly_character_ids,
            self.friendly_corporation_ids,
            self.friendly_alliance_ids,
        )
        latest_engagement = next(
            (item for item in recent_engagements if item.destroyed_count > 0),
            recent_engagements[0] if recent_engagements else None,
        )

        return AnalysisReport(
            request_id=request_id,
            requested_count=requested_count,
            resolved_count=len(identities),
            invalid_names=invalid_names,
            coverage_ratio=coverage,
            data_events=len(mails),
            engagement_count=len(engagements),
            data_window_days=effective_window_days,
            generated_at=now,
            last_activity=max((mail.killmail_time for mail in mails), default=None),
            latest_engagement=latest_engagement,
            recent_engagements=recent_engagements,
            profiles=profiles,
            affiliations=affiliations,
            role_distribution=role_distribution,
            top_ships=top_ships,
            fleet_size_windows=fleet_size_windows,
            activity_hours=activity_hours,
            activity_week_hours=activity_week_hours,
            median_gang_size=median_gang,
            p75_gang_size=p75_gang,
            solo_ratio=(weighted_solo / total_engagement_weight)
            if total_engagement_weight
            else None,
            kill_efficiency=kill_efficiency,
            destroyed_value_30d=destroyed_value_30d,
            lost_value_30d=lost_value_30d,
            isk_efficiency_30d=isk_efficiency_30d,
            isk_value_events_30d=isk_value_events_30d,
            recent_7d_kills=recent_7d_kills,
            recent_7d_losses=recent_7d_losses,
            peak_activity=peak_activity,
            fleet_size_label=fleet_size_label,
            threat_score=threat_score,
            threat_level=threat_level,
            threat_components=threat_components,
            threat_reasons=threat_reasons,
            doctrines=doctrines,
            top_systems=top_systems,
            top_regions=top_regions,
            common_associates=common_associates,
            pilot_ships=pilot_ships,
            core_members=core_members,
            engagement_patterns=self._engagement_patterns(mails, input_ids, ship_types),
            warnings=list(dict.fromkeys(warnings)),
        )

    def top_associate_ids(
        self, killmails: list[Killmail], input_ids: set[int], limit: int = 10
    ) -> list[int]:
        counts: Counter[int] = Counter()
        for engagement in _build_fleet_engagements(
            killmails,
            input_ids,
            self.friendly_character_ids,
            self.friendly_corporation_ids,
            self.friendly_alliance_ids,
        ):
            counts.update(set(engagement.ships_by_character) - input_ids)
        return [character_id for character_id, count in counts.most_common() if count >= 2][:limit]

    def _build_profiles(
        self,
        character_stats: dict[int, dict[str, object]],
        identity_by_id: dict[int, CharacterIdentity],
        ship_types: dict[int, ShipTypeInfo],
        recent_start: datetime,
    ) -> list[CharacterProfile]:
        profiles: list[CharacterProfile] = []
        for character_id, identity in identity_by_id.items():
            stats = character_stats[character_id]
            event_count = int(stats["events"])
            last_activity = stats["last"]
            if event_count >= 20 and last_activity is not None and last_activity >= recent_start:
                confidence = Confidence.HIGH
            elif event_count >= 5:
                confidence = Confidence.MEDIUM
            else:
                confidence = Confidence.LOW

            profile_warnings: list[str] = []
            if confidence == Confidence.LOW:
                profile_warnings.append("公开样本不足")
            primary_roles = _counter_metrics(stats["roles"], limit=2)
            degree = int(stats["degree"])
            candidate_label = primary_roles[0].name if primary_roles else "成员"
            if (
                any(metric.name == ShipRole.COMMAND.value for metric in primary_roles)
                and degree >= 2
            ):
                candidate_label = "指挥 / FC 候选"
            elif degree >= 3:
                candidate_label = "核心成员候选"
            profiles.append(
                CharacterProfile(
                    character_id=character_id,
                    name=identity.name,
                    corporation_id=identity.corporation_id,
                    corporation_name=identity.corporation_name,
                    corporation_ticker=identity.corporation_ticker,
                    alliance_id=identity.alliance_id,
                    alliance_name=identity.alliance_name,
                    alliance_ticker=identity.alliance_ticker,
                    birthday=identity.birthday,
                    security_status=identity.security_status,
                    event_count=event_count,
                    weighted_event_count=float(stats["weighted"]),
                    confidence=confidence,
                    last_activity=last_activity,
                    top_ships=self._ship_metrics(stats["ships"], ship_types, limit=3),
                    primary_roles=primary_roles,
                    cooccurrence_score=degree,
                    kill_count=int(stats["kills"]),
                    loss_count=int(stats["losses"]),
                    final_blow_count=int(stats["final_blows"]),
                    peak_activity=_peak_activity(stats["hours"]),
                    candidate_label=candidate_label,
                    warnings=profile_warnings,
                )
            )
        return sorted(
            profiles,
            key=lambda item: (
                item.cooccurrence_score,
                item.weighted_event_count,
                item.name.casefold(),
            ),
            reverse=True,
        )

    @staticmethod
    def _affiliations(identities: list[CharacterIdentity]) -> list[NamedMetric]:
        counts: Counter[tuple[int, str]] = Counter()
        for identity in identities:
            if identity.alliance_id and identity.alliance_name:
                counts[(identity.alliance_id, identity.alliance_name)] += 1
            else:
                counts[(identity.corporation_id, identity.corporation_name)] += 1
        return [
            NamedMetric(id=entity_id, name=name, value=float(count))
            for (entity_id, name), count in counts.most_common()
        ]

    @staticmethod
    def _ship_metrics(
        counts: Counter[int], ship_types: dict[int, ShipTypeInfo], limit: int
    ) -> list[NamedMetric]:
        return [
            NamedMetric(
                id=type_id,
                name=ship_types[type_id].name if type_id in ship_types else f"舰船 {type_id}",
                value=float(value),
            )
            for type_id, value in counts.most_common(limit)
        ]

    @staticmethod
    def _engagement_patterns(
        mails: list[Killmail], input_ids: set[int], ship_types: dict[int, ShipTypeInfo]
    ) -> list[EngagementPattern]:
        by_system: dict[int, list[Killmail]] = defaultdict(list)
        for mail in mails:
            by_system[mail.solar_system_id].append(mail)

        pattern_stats: dict[str, tuple[int, datetime]] = {}
        for system_mails in by_system.values():
            system_mails.sort(key=lambda item: item.killmail_time)
            cluster: list[Killmail] = []
            for mail in system_mails:
                if cluster and mail.killmail_time - cluster[-1].killmail_time > timedelta(
                    minutes=20
                ):
                    _record_cluster(cluster, input_ids, ship_types, pattern_stats)
                    cluster = []
                cluster.append(mail)
            _record_cluster(cluster, input_ids, ship_types, pattern_stats)

        return [
            EngagementPattern(label=label, occurrences=count, last_seen=last_seen)
            for label, (count, last_seen) in sorted(
                pattern_stats.items(), key=lambda item: (item[1][0], item[1][1]), reverse=True
            )[:5]
        ]


def _record_cluster(
    cluster: list[Killmail],
    input_ids: set[int],
    ship_types: dict[int, ShipTypeInfo],
    pattern_stats: dict[str, tuple[int, datetime]],
) -> None:
    if not cluster:
        return
    role_by_character: dict[int, ShipRole] = {}
    for mail in cluster:
        for participant in mail.participants:
            if (
                participant.character_id in input_ids
                and not participant.is_victim
                and participant.ship_type_id is not None
            ):
                role_by_character[participant.character_id] = ship_types.get(
                    participant.ship_type_id,
                    ShipTypeInfo(
                        type_id=participant.ship_type_id,
                        name="未知",
                        group_id=0,
                        group_name="未知",
                    ),
                ).role
    if len(role_by_character) < 2:
        return
    role_counts = Counter(role.value for role in role_by_character.values())
    label = " / ".join(f"{role}×{count}" for role, count in role_counts.most_common())
    previous_count, previous_last = pattern_stats.get(label, (0, datetime.min.replace(tzinfo=UTC)))
    pattern_stats[label] = (previous_count + 1, max(previous_last, cluster[-1].killmail_time))


def _build_fleet_engagements(
    mails: list[Killmail],
    input_ids: set[int],
    friendly_character_ids: frozenset[int] | set[int] = frozenset(),
    friendly_corporation_ids: frozenset[int] | set[int] = frozenset(),
    friendly_alliance_ids: frozenset[int] | set[int] = frozenset(),
) -> list[FleetEngagementSample]:
    by_system: dict[int, list[Killmail]] = defaultdict(list)
    for mail in mails:
        by_system[mail.solar_system_id].append(mail)

    samples: list[FleetEngagementSample] = []
    for system_id, system_mails in by_system.items():
        system_mails.sort(key=lambda item: item.killmail_time)
        cluster: list[Killmail] = []
        for mail in system_mails:
            if cluster and mail.killmail_time - cluster[-1].killmail_time > timedelta(minutes=20):
                sample = _fleet_sample_from_cluster(
                    system_id,
                    cluster,
                    input_ids,
                    friendly_character_ids,
                    friendly_corporation_ids,
                    friendly_alliance_ids,
                )
                if sample:
                    samples.append(sample)
                cluster = []
            cluster.append(mail)
        sample = _fleet_sample_from_cluster(
            system_id,
            cluster,
            input_ids,
            friendly_character_ids,
            friendly_corporation_ids,
            friendly_alliance_ids,
        )
        if sample:
            samples.append(sample)
    return sorted(_purify_fleet_samples(samples, input_ids), key=lambda item: item.last_seen)


def _fleet_sample_from_cluster(
    system_id: int,
    cluster: list[Killmail],
    input_ids: set[int],
    friendly_character_ids: frozenset[int] | set[int],
    friendly_corporation_ids: frozenset[int] | set[int],
    friendly_alliance_ids: frozenset[int] | set[int],
) -> FleetEngagementSample | None:
    if not cluster:
        return None
    ships_by_character: dict[int, int] = {}
    affiliations_by_character: dict[int, tuple[int | None, int | None]] = {}
    appearances: Counter[int] = Counter()
    observed_times: list[datetime] = []
    for mail in cluster:
        has_input_attacker = any(
            participant.character_id in input_ids and not participant.is_victim
            for participant in mail.participants
        )
        if not has_input_attacker:
            continue
        observed_times.append(mail.killmail_time)
        for participant in mail.participants:
            if (
                participant.is_victim
                or participant.character_id is None
                or participant.ship_type_id is None
            ):
                continue
            if participant.character_id not in input_ids and _is_friendly_participant(
                participant,
                friendly_character_ids,
                friendly_corporation_ids,
                friendly_alliance_ids,
            ):
                continue
            # Later observations win when a pilot reships inside one engagement.
            ships_by_character[participant.character_id] = participant.ship_type_id
            appearances[participant.character_id] += 1
            affiliations_by_character[participant.character_id] = (
                participant.corporation_id,
                participant.alliance_id,
            )
    if not ships_by_character:
        return None
    return FleetEngagementSample(
        system_id=system_id,
        started_at=observed_times[0],
        last_seen=observed_times[-1],
        ships_by_character=ships_by_character,
        affiliations_by_character=affiliations_by_character,
        event_count=len(observed_times),
        repeated_character_ids=frozenset(
            character_id for character_id, count in appearances.items() if count >= 2
        ),
    )


def _purify_fleet_samples(
    samples: list[FleetEngagementSample], input_ids: set[int]
) -> list[FleetEngagementSample]:
    engagement_occurrences: Counter[int] = Counter()
    for sample in samples:
        engagement_occurrences.update(sample.ships_by_character.keys())

    purified: list[FleetEngagementSample] = []
    for sample in samples:
        observed_ids = set(sample.ships_by_character)
        input_observed = observed_ids & input_ids
        if sample.event_count >= 2 and sample.repeated_character_ids:
            allowed_ids = input_observed | set(sample.repeated_character_ids)
            confidence = (
                Confidence.HIGH
                if sample.event_count >= 3 and len(sample.repeated_character_ids) >= 2
                else Confidence.MEDIUM
            )
        elif sample.event_count == 1:
            recurring_ids = {
                character_id
                for character_id in observed_ids - input_ids
                if engagement_occurrences[character_id] >= 2
            }
            allowed_ids = input_observed | recurring_ids
            confidence = Confidence.MEDIUM if recurring_ids else Confidence.LOW
        else:
            allowed_ids = input_observed
            confidence = Confidence.LOW

        ships = {
            character_id: type_id
            for character_id, type_id in sample.ships_by_character.items()
            if character_id in allowed_ids
        }
        if not ships:
            continue
        affiliations = {
            character_id: affiliation
            for character_id, affiliation in sample.affiliations_by_character.items()
            if character_id in allowed_ids
        }
        purified.append(
            replace(
                sample,
                ships_by_character=ships,
                affiliations_by_character=affiliations,
                composition_confidence=confidence,
            )
        )
    return purified


def _is_friendly_participant(
    participant: Participant,
    friendly_character_ids: frozenset[int] | set[int],
    friendly_corporation_ids: frozenset[int] | set[int],
    friendly_alliance_ids: frozenset[int] | set[int],
) -> bool:
    return bool(
        participant.character_id in friendly_character_ids
        or participant.corporation_id in friendly_corporation_ids
        or participant.alliance_id in friendly_alliance_ids
    )


def _associate_event_scores(
    mails: list[Killmail],
    input_ids: set[int],
) -> dict[int, float]:
    counts: Counter[int] = Counter()
    for mail in mails:
        attackers = {
            participant.character_id
            for participant in mail.participants
            if not participant.is_victim and participant.character_id is not None
        }
        if not attackers.intersection(input_ids):
            continue
        counts.update(attackers - input_ids)
    denominator = len(mails)
    if denominator <= 0:
        return {}
    return {
        character_id: round(count / denominator * 100, 1)
        for character_id, count in counts.items()
    }


def _pilot_ship_metrics(
    mails: list[Killmail],
    input_ids: set[int],
    ship_types: dict[int, ShipTypeInfo],
    limit: int = 10,
) -> list[PilotShipMetric]:
    kills: Counter[int] = Counter()
    losses: Counter[int] = Counter()
    for mail in mails:
        for participant in mail.participants:
            if (
                participant.character_id not in input_ids
                or participant.ship_type_id is None
            ):
                continue
            target = losses if participant.is_victim else kills
            target[participant.ship_type_id] += 1
    type_ids = set(kills) | set(losses)
    metrics = [
        PilotShipMetric(
            id=type_id,
            name=(
                ship_types[type_id].name
                if type_id in ship_types
                else f"舰船 {type_id}"
            ),
            kill_count=kills[type_id],
            loss_count=losses[type_id],
        )
        for type_id in type_ids
    ]
    return sorted(
        metrics,
        key=lambda item: (item.kill_count, item.loss_count, item.name),
        reverse=True,
    )[:limit]


def _associate_candidates(
    engagements: list[FleetEngagementSample],
    input_ids: set[int],
    identities: list[CharacterIdentity],
    associate_names: dict[int, str],
    recent_start: datetime,
) -> list[AssociateCandidate]:
    counts: Counter[int] = Counter()
    recent_counts: Counter[int] = Counter()
    days: defaultdict[int, set[date]] = defaultdict(set)
    last_seen: dict[int, datetime] = {}
    affiliations: dict[int, tuple[int | None, int | None]] = {}
    for engagement in engagements:
        for character_id in set(engagement.ships_by_character) - input_ids:
            counts[character_id] += 1
            if engagement.last_seen >= recent_start:
                recent_counts[character_id] += 1
            days[character_id].add(engagement.last_seen.astimezone(SHANGHAI).date())
            last_seen[character_id] = max(
                last_seen.get(character_id, datetime.min.replace(tzinfo=UTC)),
                engagement.last_seen,
            )
            affiliations[character_id] = engagement.affiliations_by_character.get(
                character_id, (None, None)
            )

    input_corporations = {identity.corporation_id for identity in identities}
    input_alliances = {identity.alliance_id for identity in identities if identity.alliance_id}
    candidates: list[AssociateCandidate] = []
    for character_id, count in counts.items():
        if count < 2:
            continue
        corporation_id, alliance_id = affiliations.get(character_id, (None, None))
        if corporation_id in input_corporations:
            affiliation_label = "同军团"
        elif alliance_id in input_alliances:
            affiliation_label = "同联盟"
        else:
            affiliation_label = None
        distinct_days = len(days[character_id])
        fixed = count >= 3 and distinct_days >= 2
        if affiliation_label and count >= 2 and distinct_days >= 2:
            fixed = True
        candidates.append(
            AssociateCandidate(
                id=character_id,
                name=associate_names.get(character_id, f"角色 {character_id}"),
                engagement_count=count,
                distinct_days=distinct_days,
                recent_engagement_count=recent_counts[character_id],
                relation_label="固定队友" if fixed else "经常同行",
                affiliation_label=affiliation_label,
                last_seen=last_seen[character_id],
            )
        )
    return sorted(
        candidates,
        key=lambda item: (
            item.relation_label == "固定队友",
            item.engagement_count,
            item.recent_engagement_count,
            item.distinct_days,
            item.last_seen,
        ),
        reverse=True,
    )[:10]


def _recent_battles(
    mails: list[Killmail],
    input_ids: set[int],
    ship_types: dict[int, ShipTypeInfo],
    solar_systems: dict[int, SolarSystemInfo],
    friendly_character_ids: frozenset[int] | set[int],
    friendly_corporation_ids: frozenset[int] | set[int],
    friendly_alliance_ids: frozenset[int] | set[int],
    limit: int = 90,
) -> list[LatestEngagement]:
    relevant = sorted(
        (
            mail
            for mail in mails
            if any(
                participant.character_id in input_ids
                for participant in mail.participants
            )
        ),
        key=lambda item: item.killmail_time,
    )
    clusters: list[list[Killmail]] = []
    cluster: list[Killmail] = []
    for mail in relevant:
        if (
            cluster
            and mail.killmail_time - cluster[-1].killmail_time
            > timedelta(minutes=30)
        ):
            clusters.append(cluster)
            cluster = []
        cluster.append(mail)
    if cluster:
        clusters.append(cluster)
    if not clusters:
        return []
    recent: list[LatestEngagement] = []
    for cluster in sorted(
        clusters,
        key=lambda item: item[-1].killmail_time,
        reverse=True,
    )[:limit]:
        recent.append(
            _battle_from_cluster(
                cluster,
                input_ids,
                ship_types,
                solar_systems,
                friendly_character_ids,
                friendly_corporation_ids,
                friendly_alliance_ids,
            )
        )
    return recent


def _battle_from_cluster(
    cluster: list[Killmail],
    input_ids: set[int],
    ship_types: dict[int, ShipTypeInfo],
    solar_systems: dict[int, SolarSystemInfo],
    friendly_character_ids: frozenset[int] | set[int],
    friendly_corporation_ids: frozenset[int] | set[int],
    friendly_alliance_ids: frozenset[int] | set[int],
) -> LatestEngagement:

    attacker_ships: dict[int, int] = {}
    attacker_appearances: Counter[int] = Counter()
    lost_input_ships: dict[int, int] = {}
    destroyed_targets: Counter[int] = Counter()
    lost_ships: Counter[int] = Counter()
    has_offense = False
    has_loss = False
    destroyed_count = 0
    loss_count = 0
    destroyed_value = 0.0
    lost_value = 0.0
    for mail in cluster:
        input_attackers = {
            participant.character_id
            for participant in mail.participants
            if participant.character_id in input_ids and not participant.is_victim
        }
        input_victims = {
            participant.character_id
            for participant in mail.participants
            if participant.character_id in input_ids and participant.is_victim
        }
        if input_attackers:
            has_offense = True
            destroyed_count += 1
            if mail.total_value is not None:
                destroyed_value += mail.total_value
            for participant in mail.participants:
                if participant.is_victim:
                    if participant.ship_type_id is not None:
                        destroyed_targets[participant.ship_type_id] += 1
                    continue
                if participant.character_id is None or participant.ship_type_id is None:
                    continue
                if participant.character_id not in input_ids and _is_friendly_participant(
                    participant,
                    friendly_character_ids,
                    friendly_corporation_ids,
                    friendly_alliance_ids,
                ):
                    continue
                attacker_ships[participant.character_id] = participant.ship_type_id
                attacker_appearances[participant.character_id] += 1
        if input_victims:
            has_loss = True
            loss_count += 1
            if mail.total_value is not None:
                lost_value += mail.total_value
            for participant in mail.participants:
                if (
                    participant.character_id not in input_victims
                    or participant.ship_type_id is None
                ):
                    continue
                previous_type_id = attacker_ships.get(
                    participant.character_id,
                    lost_input_ships.get(participant.character_id),
                )
                if not (
                    previous_type_id is not None
                    and not _is_capsule(previous_type_id, ship_types)
                    and _is_capsule(participant.ship_type_id, ship_types)
                ):
                    lost_input_ships[participant.character_id] = participant.ship_type_id
                lost_ships[participant.ship_type_id] += 1

    stable_ids = {
        character_id for character_id, count in attacker_appearances.items() if count >= 2
    }
    temporary_ids = set(attacker_ships) - input_ids - stable_ids
    if destroyed_count <= 1:
        selected_attacker_ids = set(attacker_ships)
        composition_confidence = Confidence.LOW
        composition_basis = "单条战报攻击方"
        composition_label = "同战报攻击方舰船（仅供参考）"
    elif stable_ids:
        selected_attacker_ids = (set(attacker_ships) & input_ids) | stable_ids
        composition_confidence = (
            Confidence.HIGH
            if destroyed_count >= 3 and len(stable_ids) >= 2
            else Confidence.MEDIUM
        )
        composition_basis = "查询角色 + 重复同场成员"
        composition_label = "重复同场的稳定同行配置"
    else:
        selected_attacker_ids = set(attacker_ships) & input_ids
        composition_confidence = Confidence.LOW
        composition_basis = "多条战报但无重复同行"
        composition_label = "查询角色舰船（同行样本不足）"
    ships_by_character = {
        character_id: type_id
        for character_id, type_id in attacker_ships.items()
        if character_id in selected_attacker_ids
    }
    ships_by_character.update(lost_input_ships)

    ships: list[FleetCompositionItem] = []
    role_counts: Counter[str] = Counter()
    for type_id, count in Counter(ships_by_character.values()).items():
        info = ship_types.get(type_id)
        role = info.role if info else ShipRole.OTHER
        ships.append(
            FleetCompositionItem(
                id=type_id,
                name=info.name if info else f"舰船 {type_id}",
                role=role.value,
                count=count,
            )
        )
        role_counts[role.value] += count
    ships.sort(key=lambda item: (item.count, item.name), reverse=True)
    roles = [
        NamedMetric(name=role, value=float(count)) for role, count in role_counts.most_common()
    ]
    if has_offense and has_loss:
        outcome = "交火并有损失"
        result_detail = (
            f"击毁 {_top_ship_name(destroyed_targets, ship_types)} · "
            f"损失 {_top_ship_name(lost_ships, ship_types)}"
        )
    elif has_offense:
        outcome = "参与击毁"
        result_detail = f"主要目标 {_top_ship_name(destroyed_targets, ship_types)}"
    else:
        outcome = "舰船损失"
        result_detail = f"损失 {_top_ship_name(lost_ships, ship_types)}"
    system_ids = list(dict.fromkeys(mail.solar_system_id for mail in cluster))
    systems = [solar_systems.get(system_id) for system_id in system_ids]
    system_names = [
        system.name if system is not None else f"星系 {system_id}"
        for system_id, system in zip(system_ids, systems, strict=True)
    ]
    display_systems = " / ".join(system_names[:2])
    if len(system_names) > 2:
        display_systems += f" / +{len(system_names) - 2}"
    regions = list(
        dict.fromkeys(
            system.region_name for system in systems if system is not None
        )
    )
    related_battle_refs = [
        RelatedBattleRef(system_id=system_id, occurred_at=occurred_at)
        for system_id, occurred_at in dict.fromkeys(
            (
                mail.solar_system_id,
                mail.killmail_time.replace(minute=0, second=0, microsecond=0),
            )
            for mail in cluster
        )
    ]
    return LatestEngagement(
        started_at=cluster[0].killmail_time,
        last_seen=cluster[-1].killmail_time,
        solar_system_id=system_ids[-1],
        system_name=display_systems,
        region_name=regions[0] if len(regions) == 1 else None,
        fleet_size=len(ships_by_character),
        event_count=len(cluster),
        outcome=outcome,
        result_detail=result_detail,
        total_value=(destroyed_value + lost_value)
        if destroyed_value + lost_value > 0
        else None,
        destroyed_count=destroyed_count,
        loss_count=loss_count,
        destroyed_value=destroyed_value,
        lost_value=lost_value,
        observed_attacker_count=len(attacker_ships),
        stable_pilot_count=len(stable_ids),
        temporary_pilot_count=len(temporary_ids),
        composition_confidence=composition_confidence,
        composition_basis=composition_basis,
        composition_label=composition_label,
        related_battle_refs=related_battle_refs,
        ships=ships,
        destroyed_ships=_ship_items(destroyed_targets, ship_types),
        lost_ships=_ship_items(lost_ships, ship_types),
        roles=roles,
    )


def _top_ship_name(counts: Counter[int], ship_types: dict[int, ShipTypeInfo]) -> str:
    if not counts:
        return "未知舰船"
    non_capsules = Counter(
        {
            type_id: count
            for type_id, count in counts.items()
            if not _is_capsule(type_id, ship_types)
        }
    )
    type_id, _ = (non_capsules or counts).most_common(1)[0]
    return ship_types[type_id].name if type_id in ship_types else f"舰船 {type_id}"


def _ship_items(
    counts: Counter[int],
    ship_types: dict[int, ShipTypeInfo],
) -> list[FleetCompositionItem]:
    items: list[FleetCompositionItem] = []
    for type_id, count in counts.most_common(4):
        info = ship_types.get(type_id)
        items.append(
            FleetCompositionItem(
                id=type_id,
                name=info.name if info else f"舰船 {type_id}",
                role=(info.role if info else ShipRole.OTHER).value,
                count=count,
            )
        )
    return items


def _is_capsule(type_id: int, ship_types: dict[int, ShipTypeInfo]) -> bool:
    info = ship_types.get(type_id)
    if info is None:
        return type_id == 670
    return bool(
        info.group_id == 29
        or info.name in {"太空舱", "Capsule"}
        or info.group_name.casefold() == "capsule"
        or (info.group_name_en or "").casefold() == "capsule"
    )


def _role_composition_metrics(
    engagements: list[FleetEngagementSample],
    weights: list[int],
    ship_types: dict[int, ShipTypeInfo],
) -> list[CompositionMetric]:
    if not engagements:
        return []
    per_engagement: list[Counter[str]] = []
    role_names: set[str] = set()
    for engagement in engagements:
        counts: Counter[str] = Counter()
        for type_id in engagement.ships_by_character.values():
            info = ship_types.get(type_id)
            role = info.role if info else ShipRole.OTHER
            counts[role.value] += 1
        per_engagement.append(counts)
        role_names.update(counts)

    total_weight = sum(weights) or 1
    metrics: list[CompositionMetric] = []
    for role_name in role_names:
        values = _weighted_values(
            [float(counts.get(role_name, 0)) for counts in per_engagement], weights
        )
        present_weight = sum(
            weight
            for counts, weight in zip(per_engagement, weights, strict=True)
            if counts.get(role_name, 0) > 0
        )
        metrics.append(
            CompositionMetric(
                name=role_name,
                median=median(values),
                p75=_percentile(values, 0.75),
                occurrence_rate=present_weight / total_weight,
                sample_count=len(engagements),
            )
        )
    return sorted(
        metrics,
        key=lambda item: (item.p75, item.median, item.occurrence_rate),
        reverse=True,
    )


def _ship_composition_metrics(
    engagements: list[FleetEngagementSample],
    weights: list[int],
    ship_types: dict[int, ShipTypeInfo],
    limit: int,
) -> list[CompositionMetric]:
    if not engagements:
        return []
    per_engagement = [item.ship_counts for item in engagements]
    type_ids = {type_id for counts in per_engagement for type_id in counts}
    total_weight = sum(weights) or 1
    metrics: list[CompositionMetric] = []
    for type_id in type_ids:
        present_values: list[float] = []
        present_weight = 0
        for counts, weight in zip(per_engagement, weights, strict=True):
            value = float(counts.get(type_id, 0))
            if not value:
                continue
            present_values.extend([value] * weight)
            present_weight += weight
        if not present_values:
            continue
        info = ship_types.get(type_id)
        metrics.append(
            CompositionMetric(
                id=type_id,
                name=info.name if info else f"舰船 {type_id}",
                role=info.role.value if info else ShipRole.OTHER.value,
                median=median(present_values),
                p75=_percentile(present_values, 0.75),
                occurrence_rate=present_weight / total_weight,
                sample_count=len(engagements),
            )
        )
    return sorted(
        metrics,
        key=lambda item: (item.occurrence_rate, item.median, item.p75),
        reverse=True,
    )[:limit]


def _aggregate_engagement_counts(
    engagements: list[FleetEngagementSample],
    weights: list[int],
    ship_types: dict[int, ShipTypeInfo],
) -> tuple[Counter[str], Counter[int]]:
    role_counts: Counter[str] = Counter()
    ship_counts: Counter[int] = Counter()
    for engagement, weight in zip(engagements, weights, strict=True):
        for type_id in engagement.ships_by_character.values():
            info = ship_types.get(type_id)
            role = info.role if info else ShipRole.OTHER
            ship_counts[type_id] += weight
            role_counts[role.value] += weight
    return role_counts, ship_counts


def _weighted_values(values: list[float | int], weights: list[int]) -> list[float]:
    if len(values) != len(weights):
        raise ValueError("Values and weights must have identical lengths")
    return [
        float(value) for value, weight in zip(values, weights, strict=True) for _ in range(weight)
    ]


def _location_metrics(
    system_counts: Counter[int], solar_systems: dict[int, SolarSystemInfo]
) -> tuple[list[NamedMetric], list[NamedMetric]]:
    systems: list[NamedMetric] = []
    regions: Counter[tuple[int, str]] = Counter()
    for system_id, count in system_counts.most_common():
        info = solar_systems.get(system_id)
        name = info.name if info else f"星系 {system_id}"
        systems.append(NamedMetric(id=system_id, name=name, value=float(count)))
        if info:
            regions[(info.region_id, info.region_name)] += count
    region_metrics = [
        NamedMetric(id=region_id, name=name, value=float(count))
        for (region_id, name), count in regions.most_common(8)
    ]
    return systems[:8], region_metrics


def _threat_rating(
    *,
    event_count: int,
    recent_kills: int,
    recent_losses: int,
    kill_efficiency: float | None,
    isk_efficiency_30d: float | None,
    isk_value_events_30d: int,
    p75_gang_size: float | None,
    fleet_roles: Counter[str],
    doctrines: list[DoctrineMatch],
    ship_counts: Counter[int],
    ship_types: dict[int, ShipTypeInfo],
    peak_activity: str,
) -> tuple[int, str, list[ThreatComponent], list[str]]:
    recent_events = recent_kills + recent_losses
    activity_score = min(25, round(recent_events * 2.5))
    if p75_gang_size is None:
        scale_score = 0
    elif p75_gang_size <= 1:
        scale_score = 3
    elif p75_gang_size <= 5:
        scale_score = 8
    elif p75_gang_size <= 15:
        scale_score = 13
    elif p75_gang_size <= 40:
        scale_score = 17
    else:
        scale_score = 20
    effectiveness = (
        isk_efficiency_30d if isk_efficiency_30d is not None else kill_efficiency
    )
    effectiveness_samples = isk_value_events_30d or recent_events or event_count
    combat_sample_factor = min(1.0, effectiveness_samples / 10)
    effectiveness_score = round((effectiveness or 0.0) * combat_sample_factor * 20)
    if doctrines:
        doctrine_confidence = max(item.confidence for item in doctrines)
        doctrine_score = round(doctrine_confidence / 100 * 15)
    else:
        meaningful_roles = {
            name
            for name, value in fleet_roles.items()
            if value > 0 and name != ShipRole.OTHER.value
        }
        support_roles = sum(
            fleet_roles[role.value]
            for role in (ShipRole.LOGISTICS, ShipRole.EWAR, ShipRole.COMMAND, ShipRole.INTERDICTION)
        )
        role_total = sum(fleet_roles.values()) or 1
        doctrine_score = min(15, len(meaningful_roles) * 2 + round(support_roles / role_total * 10))
    capital_count = sum(
        count
        for type_id, count in ship_counts.items()
        if ship_types.get(type_id) and ship_types[type_id].role == ShipRole.CAPITAL
    )
    capital_score = 0 if not capital_count else min(10, 4 + round(capital_count))
    sample_score = min(10, round(event_count / 3))

    components = [
        ThreatComponent(
            name="近期活跃",
            score=activity_score,
            maximum=25,
            explanation=f"近7天 {recent_events} 次公开战斗事件",
        ),
        ThreatComponent(
            name="舰队规模",
            score=scale_score,
            maximum=20,
            explanation=(
                f"规模较大时约 {p75_gang_size:.0f} 人"
                if p75_gang_size is not None
                else "缺少进攻舰队样本"
            ),
        ),
        ThreatComponent(
            name="战损表现",
            score=effectiveness_score,
            maximum=20,
            explanation=(
                f"近30天 ISK 效率 {isk_efficiency_30d:.0%}"
                if isk_efficiency_30d is not None
                else f"价值样本不足，事件占比 {kill_efficiency:.0%}"
                if kill_efficiency is not None
                else "缺少击杀/损失样本"
            ),
        ),
        ThreatComponent(
            name="体系完整度",
            score=doctrine_score,
            maximum=15,
            explanation=(doctrines[0].name if doctrines else "按舰队角色多样性计算"),
        ),
        ThreatComponent(
            name="旗舰风险",
            score=capital_score,
            maximum=10,
            explanation=f"观察到 {capital_count:.0f} 次旗舰出场"
            if capital_count
            else "未观察到旗舰",
        ),
        ThreatComponent(
            name="样本稳定性",
            score=sample_score,
            maximum=10,
            explanation=f"近90天 {event_count} 个去重事件",
        ),
    ]
    score = sum(item.score for item in components)
    if score >= 85:
        level = "极高"
    elif score >= 70:
        level = "很高"
    elif score >= 50:
        level = "高"
    elif score >= 25:
        level = "中"
    else:
        level = "低"
    reasons = [
        f"近7天参与击毁 {recent_kills} 次",
        (
            f"规模较大时约 {p75_gang_size:.0f} 人"
            if p75_gang_size is not None
            else "舰队规模样本不足"
        ),
        f"主要活跃时段 {peak_activity}",
    ]
    if doctrines:
        reasons.insert(1, f"疑似 {doctrines[0].name}（{doctrines[0].confidence}%）")
    if capital_count:
        reasons.append(f"观察到 {capital_count:.0f} 次旗舰出场")
    return score, level, components, reasons[:5]


def _peak_activity(values: list[float]) -> str:
    if not values or not any(values):
        return "样本不足"
    doubled = values + values
    window = 4
    start = max(range(24), key=lambda hour: sum(doubled[hour : hour + window]))
    end = (start + window) % 24
    return f"{start:02d}:00–{end:02d}:00"


def _fleet_size_label(median_size: float | None, p75_size: float | None) -> str:
    if median_size is None or p75_size is None:
        return "样本不足"
    if p75_size <= 1:
        return "单收 / 1人"
    if p75_size <= 5:
        return "小队 / 2–5人"
    if p75_size <= 10:
        return "小队 / 5–10人"
    if p75_size <= 20:
        return "中队 / 5–20人"
    if p75_size <= 50:
        return "舰队 / 10–50人"
    return "大型舰队 / 50+人"


_FLEET_SIZE_BUCKETS = ("0–4人", "4–8人", "8–12人", "12人以上")


def _fleet_size_windows(
    mails: list[Killmail],
    input_ids: set[int],
    friendly_character_ids: frozenset[int] | set[int],
    friendly_corporation_ids: frozenset[int] | set[int],
    friendly_alliance_ids: frozenset[int] | set[int],
    now: datetime,
) -> list[FleetSizeWindow]:
    """Summarize attacker group sizes for stable KM and time windows."""
    ordered = sorted(mails, key=lambda item: _aware(item.killmail_time), reverse=True)
    windows = (
        ("最近 30 个 KM", ordered[:30]),
        ("近 30 天", [mail for mail in ordered if _aware(mail.killmail_time) >= now - timedelta(days=30)]),
        ("近 90 天", [mail for mail in ordered if _aware(mail.killmail_time) >= now - timedelta(days=90)]),
    )
    result: list[FleetSizeWindow] = []
    for label, window_mails in windows:
        sizes: list[int] = []
        for mail in window_mails:
            size = _fleet_size_for_killmail(
                mail,
                input_ids,
                friendly_character_ids,
                friendly_corporation_ids,
                friendly_alliance_ids,
            )
            if size is not None:
                sizes.append(size)
        sample_count = len(sizes)
        bucket_counts = [
            sum(1 for size in sizes if _fleet_size_bucket_index(size) == index)
            for index in range(len(_FLEET_SIZE_BUCKETS))
        ]
        result.append(
            FleetSizeWindow(
                label=label,
                sample_count=sample_count,
                buckets=[
                    FleetSizeBucket(
                        label=bucket_label,
                        count=count,
                        share=count / sample_count if sample_count else 0,
                    )
                    for bucket_label, count in zip(
                        _FLEET_SIZE_BUCKETS, bucket_counts, strict=True
                    )
                ],
            )
        )
    return result


def _fleet_size_for_killmail(
    mail: Killmail,
    input_ids: set[int],
    friendly_character_ids: frozenset[int] | set[int],
    friendly_corporation_ids: frozenset[int] | set[int],
    friendly_alliance_ids: frozenset[int] | set[int],
) -> int | None:
    attackers = {
        participant.character_id
        for participant in mail.participants
        if participant.character_id is not None
        and not participant.is_victim
        and (
            participant.character_id in input_ids
            or not _is_friendly_participant(
                participant,
                friendly_character_ids,
                friendly_corporation_ids,
                friendly_alliance_ids,
            )
        )
    }
    if not attackers.intersection(input_ids):
        return None
    return len(attackers)


def _fleet_size_bucket_index(size: int) -> int:
    if size <= 4:
        return 0
    if size <= 8:
        return 1
    if size <= 12:
        return 2
    return 3


def _counter_metrics(counter: Counter[str], limit: int | None = None) -> list[NamedMetric]:
    items = counter.most_common(limit)
    return [NamedMetric(name=name, value=float(value)) for name, value in items]


def _percentile(values: list[float | int], fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    remainder = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * remainder


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
