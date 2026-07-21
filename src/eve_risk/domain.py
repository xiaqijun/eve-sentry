from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class ShipRole(StrEnum):
    DPS = "输出舰"
    LOGISTICS = "后勤"
    EWAR = "电子战"
    TACKLE = "抓人"
    INTERDICTION = "拦截"
    COMMAND = "指挥"
    SCOUT = "侦察"
    CAPITAL = "旗舰"
    INDUSTRIAL = "工业"
    OTHER = "其他"


class Confidence(StrEnum):
    HIGH = "高"
    MEDIUM = "中"
    LOW = "低"


class CharacterIdentity(BaseModel):
    character_id: int
    name: str
    corporation_id: int
    corporation_name: str = "未知军团"
    corporation_ticker: str = ""
    alliance_id: int | None = None
    alliance_name: str | None = None
    alliance_ticker: str | None = None
    birthday: datetime | None = None
    security_status: float | None = None


class ShipTypeInfo(BaseModel):
    type_id: int
    name: str
    name_en: str | None = None
    group_id: int
    group_name: str
    group_name_en: str | None = None
    category_id: int | None = None
    role: ShipRole = ShipRole.OTHER


class SolarSystemInfo(BaseModel):
    solar_system_id: int
    name: str
    region_id: int
    region_name: str


class Participant(BaseModel):
    character_id: int | None = None
    corporation_id: int | None = None
    alliance_id: int | None = None
    ship_type_id: int | None = None
    is_victim: bool = False
    final_blow: bool = False


class Killmail(BaseModel):
    killmail_id: int
    killmail_time: datetime
    solar_system_id: int
    participants: list[Participant] = Field(default_factory=list)
    solo: bool = False
    total_value: float | None = None

    def model_post_init(self, __context: object) -> None:
        if self.killmail_time.tzinfo is None:
            self.killmail_time = self.killmail_time.replace(tzinfo=UTC)


class NamedMetric(BaseModel):
    id: int | None = None
    name: str
    value: float


class CompositionMetric(BaseModel):
    id: int | None = None
    name: str
    role: str | None = None
    median: float
    p75: float
    occurrence_rate: float
    sample_count: int


class FleetCompositionItem(BaseModel):
    id: int | None = None
    name: str
    role: str
    count: int


class RelatedBattleRef(BaseModel):
    system_id: int
    occurred_at: datetime


class RelatedBattleSide(BaseModel):
    character_ids: set[int] = Field(default_factory=set)
    loss_value: float = 0
    ships_lost: int = 0
    pilot_count: int = 0
    lost_ships: list[FleetCompositionItem] = Field(default_factory=list)


class RelatedBattleSummary(BaseModel):
    system_id: int
    occurred_at: datetime
    team_a: RelatedBattleSide
    team_b: RelatedBattleSide


class LatestEngagement(BaseModel):
    started_at: datetime
    last_seen: datetime
    solar_system_id: int
    system_name: str
    region_name: str | None = None
    fleet_size: int
    event_count: int
    outcome: str = "交战"
    result_detail: str = ""
    total_value: float | None = None
    destroyed_count: int = 0
    loss_count: int = 0
    destroyed_value: float = 0
    lost_value: float = 0
    observed_attacker_count: int = 0
    stable_pilot_count: int = 0
    temporary_pilot_count: int = 0
    composition_confidence: Confidence = Confidence.LOW
    composition_basis: str = "同战报攻击方"
    composition_label: str = "观察编队"
    related_battle_refs: list[RelatedBattleRef] = Field(default_factory=list)
    ships: list[FleetCompositionItem] = Field(default_factory=list)
    destroyed_ships: list[FleetCompositionItem] = Field(default_factory=list)
    lost_ships: list[FleetCompositionItem] = Field(default_factory=list)
    roles: list[NamedMetric] = Field(default_factory=list)


class AssociateCandidate(BaseModel):
    id: int
    name: str
    engagement_count: int
    distinct_days: int
    recent_engagement_count: int
    relation_label: str
    affiliation_label: str | None = None
    last_seen: datetime
    score: float = 0


class PilotShipMetric(BaseModel):
    id: int
    name: str
    kill_count: int = 0
    loss_count: int = 0


class CharacterProfile(BaseModel):
    character_id: int
    name: str
    corporation_id: int | None = None
    corporation_name: str
    corporation_ticker: str = ""
    alliance_id: int | None = None
    alliance_name: str | None = None
    alliance_ticker: str | None = None
    birthday: datetime | None = None
    security_status: float | None = None
    event_count: int = 0
    weighted_event_count: float = 0
    confidence: Confidence = Confidence.LOW
    last_activity: datetime | None = None
    top_ships: list[NamedMetric] = Field(default_factory=list)
    primary_roles: list[NamedMetric] = Field(default_factory=list)
    cooccurrence_score: int = 0
    kill_count: int = 0
    loss_count: int = 0
    final_blow_count: int = 0
    peak_activity: str = "样本不足"
    candidate_label: str = "成员"
    warnings: list[str] = Field(default_factory=list)


class EngagementPattern(BaseModel):
    label: str
    occurrences: int
    last_seen: datetime


class DoctrineMatch(BaseModel):
    name: str
    confidence: int
    encounter_count: int = 0
    sample_count: int = 0
    evidence: list[str] = Field(default_factory=list)


class ThreatComponent(BaseModel):
    name: str
    score: int
    maximum: int
    explanation: str


class ZKillStats(BaseModel):
    character_id: int = 0
    ships_destroyed: int = 0
    ships_lost: int = 0
    points_destroyed: int = 0
    isk_destroyed: float = 0
    isk_lost: float = 0
    solo_kills: int = 0
    danger_ratio: float = 0
    gang_ratio: float = 0


class AnalysisReport(BaseModel):
    request_id: str
    requested_count: int
    resolved_count: int
    invalid_names: list[str] = Field(default_factory=list)
    coverage_ratio: float = 0
    data_events: int = 0
    engagement_count: int = 0
    data_window_days: int = 90
    generated_at: datetime
    last_activity: datetime | None = None
    latest_engagement: LatestEngagement | None = None
    recent_engagements: list[LatestEngagement] = Field(default_factory=list)
    profiles: list[CharacterProfile] = Field(default_factory=list)
    affiliations: list[NamedMetric] = Field(default_factory=list)
    role_distribution: list[CompositionMetric] = Field(default_factory=list)
    top_ships: list[CompositionMetric] = Field(default_factory=list)
    activity_hours: list[float] = Field(default_factory=lambda: [0.0] * 24)
    activity_week_hours: list[list[float]] = Field(
        default_factory=lambda: [[0.0] * 24 for _ in range(7)]
    )
    median_gang_size: float | None = None
    p75_gang_size: float | None = None
    solo_ratio: float | None = None
    kill_efficiency: float | None = None
    destroyed_value_30d: float = 0
    lost_value_30d: float = 0
    isk_efficiency_30d: float | None = None
    isk_value_events_30d: int = 0
    recent_7d_kills: int = 0
    recent_7d_losses: int = 0
    peak_activity: str = "样本不足"
    fleet_size_label: str = "样本不足"
    threat_score: int = 0
    threat_level: str = "低"
    threat_components: list[ThreatComponent] = Field(default_factory=list)
    threat_reasons: list[str] = Field(default_factory=list)
    lifetime_stats: ZKillStats | None = None
    doctrines: list[DoctrineMatch] = Field(default_factory=list)
    top_systems: list[NamedMetric] = Field(default_factory=list)
    top_regions: list[NamedMetric] = Field(default_factory=list)
    common_associates: list[AssociateCandidate] = Field(default_factory=list)
    pilot_ships: list[PilotShipMetric] = Field(default_factory=list)
    core_members: list[NamedMetric] = Field(default_factory=list)
    engagement_patterns: list[EngagementPattern] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def low_confidence_count(self) -> int:
        return sum(profile.confidence == Confidence.LOW for profile in self.profiles)


class AnalysisRequest(BaseModel):
    request_id: str
    msg_id: str
    group_openid: str
    member_openid: str
    character_names: list[str]
    received_at: datetime
    fetch_deadline_at: datetime
    reply_deadline_at: datetime
