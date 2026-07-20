from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from eve_risk.domain import (
    AnalysisReport,
    AssociateCandidate,
    CharacterProfile,
    CompositionMetric,
    Confidence,
    DoctrineMatch,
    FleetCompositionItem,
    LatestEngagement,
    NamedMetric,
    ThreatComponent,
)
from eve_risk.report import ReportRenderer


def main() -> None:
    now = datetime.now(UTC)
    hours = [1, 0, 0, 0, 0, 0, 1, 2, 3, 4, 5, 8, 10, 14, 17, 20, 26, 31, 38, 44, 50, 47, 30, 14]
    week = [[value * (0.35 + day * 0.08) for value in hours] for day in range(7)]
    profiles = [
        CharacterProfile(
            character_id=index,
            name=name,
            corporation_name="Dracarys. Fleet Operations",
            alliance_name="Dracarys.",
            event_count=42 - index * 5,
            weighted_event_count=68 - index * 7,
            confidence=Confidence.HIGH if index < 3 else Confidence.MEDIUM,
            last_activity=now - timedelta(minutes=15 + index * 30),
            top_ships=[
                NamedMetric(name="缪宁级", value=18),
                NamedMetric(name="阿施塔特级", value=8),
                NamedMetric(name="飓风级", value=6),
            ],
            primary_roles=[NamedMetric(name="输出舰", value=30)],
            cooccurrence_score=19 - index * 3,
            kill_count=35 - index * 4,
            loss_count=7 + index,
            final_blow_count=8 - index,
            peak_activity="20:00–00:00",
            candidate_label="指挥 / FC 候选" if index == 0 else "核心成员候选",
        )
        for index, name in enumerate(
            ["China CN", "Night Hunter", "Silent Spear", "Red Comet"], start=1
        )
    ]
    report = AnalysisReport(
        request_id="preview",
        requested_count=4,
        resolved_count=4,
        coverage_ratio=0.92,
        data_events=86,
        engagement_count=9,
        generated_at=now,
        last_activity=now - timedelta(minutes=15),
        latest_engagement=LatestEngagement(
            started_at=now - timedelta(minutes=31),
            last_seen=now - timedelta(minutes=15),
            solar_system_id=30002410,
            system_name="LXQ2-T",
            region_name="静寂谷",
            fleet_size=19,
            event_count=4,
            outcome="参与击杀",
            result_detail="击毁 毒蜥级",
            total_value=2_630_000_000,
            composition_label="观察到的进攻编队",
            ships=[
                FleetCompositionItem(name="缪宁级", role="输出舰", count=12),
                FleetCompositionItem(name="曲剑级", role="后勤", count=3),
                FleetCompositionItem(name="休津级", role="电子战", count=2),
                FleetCompositionItem(name="短剑级", role="抓人", count=1),
                FleetCompositionItem(name="剑齿虎级", role="拦截", count=1),
            ],
            roles=[
                NamedMetric(name="输出舰", value=12),
                NamedMetric(name="后勤", value=3),
                NamedMetric(name="电子战", value=2),
                NamedMetric(name="抓人", value=1),
                NamedMetric(name="拦截", value=1),
            ],
        ),
        profiles=profiles,
        role_distribution=[
            CompositionMetric(name="输出舰", median=9, p75=15, occurrence_rate=1, sample_count=9),
            CompositionMetric(name="电子战", median=2, p75=4, occurrence_rate=0.78, sample_count=9),
            CompositionMetric(name="侦察", median=1, p75=2, occurrence_rate=0.67, sample_count=9),
            CompositionMetric(name="后勤", median=2, p75=3, occurrence_rate=0.89, sample_count=9),
            CompositionMetric(name="指挥", median=1, p75=1, occurrence_rate=0.56, sample_count=9),
        ],
        top_ships=[
            CompositionMetric(
                name="缪宁级",
                role="输出舰",
                median=12,
                p75=18,
                occurrence_rate=0.89,
                sample_count=9,
            ),
            CompositionMetric(
                name="曲剑级", role="后勤", median=3, p75=5, occurrence_rate=0.78, sample_count=9
            ),
            CompositionMetric(
                name="休津级",
                role="电子战",
                median=2,
                p75=3,
                occurrence_rate=0.67,
                sample_count=9,
            ),
            CompositionMetric(
                name="短剑级", role="抓人", median=1, p75=2, occurrence_rate=0.67, sample_count=9
            ),
            CompositionMetric(
                name="剑齿虎级",
                role="拦截",
                median=1,
                p75=2,
                occurrence_rate=0.56,
                sample_count=9,
            ),
            CompositionMetric(
                name="洛基级",
                role="电子战",
                median=1,
                p75=1,
                occurrence_rate=0.44,
                sample_count=9,
            ),
        ],
        activity_hours=hours,
        activity_week_hours=week,
        median_gang_size=11,
        p75_gang_size=18,
        solo_ratio=0.08,
        kill_efficiency=0.72,
        recent_7d_kills=38,
        recent_7d_losses=9,
        peak_activity="20:00–00:00",
        fleet_size_label="中队 / 5–20人",
        threat_score=82,
        threat_level="很高",
        doctrines=[
            DoctrineMatch(
                name="缪宁舰队",
                confidence=82,
                encounter_count=6,
                sample_count=9,
                evidence=["缪宁级 通常12 · 大场18", "曲剑级 通常3 · 大场5"],
            ),
            DoctrineMatch(
                name="高速游击队",
                confidence=64,
                encounter_count=2,
                sample_count=9,
                evidence=["洛基级 通常1 · 大场1", "短剑级 通常1 · 大场2"],
            ),
        ],
        top_regions=[
            NamedMetric(name="静寂谷", value=48),
            NamedMetric(name="波赫文", value=22),
            NamedMetric(name="伏尔戈", value=16),
        ],
        top_systems=[
            NamedMetric(name="LXQ2-T", value=15),
            NamedMetric(name="吉他", value=12),
            NamedMetric(name="阿玛", value=8),
            NamedMetric(name="瑟斯佩特", value=6),
        ],
        common_associates=[
            AssociateCandidate(
                id=101,
                name="Azure Lance",
                engagement_count=7,
                distinct_days=5,
                recent_engagement_count=5,
                relation_label="固定队友",
                affiliation_label="同联盟",
                last_seen=now - timedelta(minutes=15),
            ),
            AssociateCandidate(
                id=102,
                name="Black Raven",
                engagement_count=5,
                distinct_days=3,
                recent_engagement_count=4,
                relation_label="固定队友",
                last_seen=now - timedelta(hours=3),
            ),
            AssociateCandidate(
                id=103,
                name="Wind Runner",
                engagement_count=2,
                distinct_days=1,
                recent_engagement_count=2,
                relation_label="经常同行",
                last_seen=now - timedelta(days=2),
            ),
            AssociateCandidate(
                id=104,
                name="Kite Master",
                engagement_count=2,
                distinct_days=2,
                recent_engagement_count=1,
                relation_label="经常同行",
                last_seen=now - timedelta(days=8),
            ),
        ],
        threat_components=[
            ThreatComponent(
                name="近期活跃", score=25, maximum=25, explanation="近7天 47 次公开战斗事件"
            ),
            ThreatComponent(
                name="舰队规模", score=17, maximum=20, explanation="规模较大时约 18 人"
            ),
            ThreatComponent(name="击杀效率", score=14, maximum=20, explanation="公开击杀占比 72%"),
            ThreatComponent(name="体系完整度", score=12, maximum=15, explanation="缪宁舰队"),
            ThreatComponent(
                name="旗舰风险", score=5, maximum=10, explanation="观察到 1 次旗舰出场"
            ),
            ThreatComponent(
                name="样本稳定性", score=9, maximum=10, explanation="近90天 86 个去重事件"
            ),
        ],
        threat_reasons=[
            "近7天击杀 38 / 损失 9",
            "疑似缪宁舰队（82%）",
            "规模较大时约 18 人",
            "主要活跃时段 20:00–00:00",
        ],
        warnings=["2 个角色的一个抓取方向失败，覆盖率为 92%"],
    )
    output = Path("reports/report-preview.png")
    output.parent.mkdir(exist_ok=True)
    output.write_bytes(ReportRenderer().render(report))
    print(output.resolve())


if __name__ == "__main__":
    main()
