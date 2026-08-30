from datetime import timedelta

from eve_risk.analysis import FleetAnalyzer
from eve_risk.domain import (
    CharacterIdentity,
    Killmail,
    Participant,
    ShipRole,
    ShipTypeInfo,
    SolarSystemInfo,
)


def _mail(mail_id, at, participants, *, solo=False, system=30000142, value=None):
    return Killmail(
        killmail_id=mail_id,
        killmail_time=at,
        solar_system_id=system,
        participants=participants,
        solo=solo,
        total_value=value,
    )


def test_analysis_deduplicates_weights_and_builds_patterns(now, identities, ship_types) -> None:
    recent_group = _mail(
        1,
        now - timedelta(days=2),
        [
            Participant(character_id=9, ship_type_id=999, is_victim=True),
            Participant(character_id=1, ship_type_id=1001),
            Participant(character_id=2, ship_type_id=1002),
            Participant(character_id=3, ship_type_id=1001),
        ],
    )
    solo = _mail(
        2,
        now - timedelta(days=5),
        [
            Participant(character_id=8, ship_type_id=999, is_victim=True),
            Participant(character_id=1, ship_type_id=1001),
        ],
        solo=True,
    )
    older_loss = _mail(
        3,
        now - timedelta(days=45),
        [
            Participant(character_id=2, ship_type_id=1002, is_victim=True),
            Participant(character_id=7, ship_type_id=1001),
        ],
    )

    report = FleetAnalyzer().analyze(
        request_id="job-1",
        requested_count=2,
        identities=identities,
        invalid_names=[],
        killmails=[recent_group, recent_group, solo, older_loss],
        ship_types=ship_types,
        covered_character_ids={1, 2},
        associate_names={3: "Outside Friend"},
        now=now,
    )

    assert report.data_events == 3
    assert report.engagement_count == 2
    assert report.coverage_ratio == 1
    assert report.role_distribution[0].name == "输出舰"
    assert report.role_distribution[0].median == 1
    assert report.role_distribution[0].p75 == 1
    assert report.common_associates == []
    assert report.solo_ratio == 0.5
    assert report.median_gang_size == 1.5
    assert report.p75_gang_size == 1.75
    assert report.kill_efficiency == 2 / 3
    assert report.engagement_patterns[0].label == "输出舰×1 / 后勤×1"
    assert report.core_members[0].value == 1
    assert report.latest_engagement is not None
    assert report.latest_engagement.last_seen == recent_group.killmail_time
    assert report.latest_engagement.fleet_size == 3
    fleet_windows = {item.label: item for item in report.fleet_size_windows}
    assert [item.label for item in report.fleet_size_windows] == [
        "最近 30 个 KM",
        "近 7 天",
        "近 30 天",
        "近 90 天",
    ]
    assert fleet_windows["最近 30 个 KM"].sample_count == 2
    assert fleet_windows["近 7 天"].sample_count == 2
    assert [bucket.count for bucket in fleet_windows["最近 30 个 KM"].buckets] == [2, 0, 0, 0]


def test_fleet_size_windows_use_km_and_time_boundaries(now, identities, ship_types) -> None:
    sizes = [1, 4, 5, 8, 9, 12, 13]
    mails = [
        _mail(
            index,
            now - timedelta(days=45 if index == 7 else index),
            [Participant(character_id=1)]
            + [Participant(character_id=1000 + index * 20 + offset) for offset in range(size - 1)],
        )
        for index, size in enumerate(sizes, start=1)
    ]
    report = FleetAnalyzer().analyze(
        request_id="fleet-size-windows",
        requested_count=1,
        identities=identities[:1],
        invalid_names=[],
        killmails=mails,
        ship_types=ship_types,
        covered_character_ids={1},
        now=now,
    )

    windows = {item.label: item for item in report.fleet_size_windows}
    assert [bucket.count for bucket in windows["最近 30 个 KM"].buckets] == [2, 2, 2, 1]
    assert windows["近 7 天"].sample_count == 6
    assert windows["近 30 天"].sample_count == 6
    assert windows["近 90 天"].sample_count == 7


def test_same_engagement_does_not_multiply_fleet_composition(now, identities, ship_types) -> None:
    mails = [
        _mail(
            index,
            now - timedelta(minutes=15 - index * 5),
            [
                Participant(character_id=900 + index, is_victim=True),
                Participant(character_id=1, ship_type_id=1001),
                Participant(character_id=3, ship_type_id=1001),
                Participant(character_id=4, ship_type_id=1002),
            ],
        )
        for index in range(1, 4)
    ]

    report = FleetAnalyzer().analyze(
        request_id="one-engagement",
        requested_count=2,
        identities=identities,
        invalid_names=[],
        killmails=mails,
        ship_types=ship_types,
        covered_character_ids={1, 2},
        now=now,
    )

    roles = {metric.name: metric for metric in report.role_distribution}
    ships = {metric.id: metric for metric in report.top_ships}
    assert report.data_events == 3
    assert report.engagement_count == 1
    assert roles["输出舰"].median == 2
    assert roles["输出舰"].p75 == 2
    assert roles["后勤"].median == 1
    assert ships[1001].median == 2
    assert ships[1001].p75 == 2
    assert report.median_gang_size == 3
    assert report.latest_engagement is not None
    assert report.latest_engagement.event_count == 3
    assert report.latest_engagement.fleet_size == 3
    latest_ships = {item.id: item.count for item in report.latest_engagement.ships}
    assert latest_ships == {1001: 2, 1002: 1}


def test_single_character_hides_one_engagement_third_party(now, identities, ship_types) -> None:
    mails = [
        _mail(
            index,
            now - timedelta(minutes=15 - index * 5),
            [
                Participant(character_id=900 + index, is_victim=True),
                Participant(character_id=1, ship_type_id=1001),
                Participant(character_id=3, ship_type_id=1002),
            ],
        )
        for index in range(1, 4)
    ]
    analyzer = FleetAnalyzer()

    report = analyzer.analyze(
        request_id="single-character",
        requested_count=1,
        identities=identities[:1],
        invalid_names=[],
        killmails=mails,
        ship_types=ship_types,
        covered_character_ids={1},
        associate_names={3: "Possible Wingmate"},
        now=now,
    )

    assert report.common_associates == []
    assert analyzer.top_associate_ids(mails, {1}) == []
    assert report.profiles[0].cooccurrence_score == 1


def test_probable_teammates_have_explainable_confidence_levels(now, identities, ship_types) -> None:
    mails = []
    for index, days_ago in enumerate((1, 2, 3), start=1):
        participants = [
            Participant(character_id=900 + index, is_victim=True),
            Participant(character_id=1, corporation_id=101, alliance_id=201, ship_type_id=1001),
            Participant(character_id=3, corporation_id=303, ship_type_id=1002),
        ]
        if index <= 2:
            participants.append(Participant(character_id=4, corporation_id=404, ship_type_id=1001))
            participants.append(
                Participant(character_id=5, corporation_id=101, alliance_id=201, ship_type_id=1001)
            )
        mails.append(_mail(300 + index, now - timedelta(days=days_ago), participants))

    analyzer = FleetAnalyzer()
    report = analyzer.analyze(
        request_id="associate-confidence",
        requested_count=1,
        identities=identities[:1],
        invalid_names=[],
        killmails=mails,
        ship_types=ship_types,
        covered_character_ids={1},
        associate_names={3: "Fixed Wingmate", 4: "Frequent Wingmate", 5: "Corp Wingmate"},
        now=now,
    )

    candidates = {item.id: item for item in report.common_associates}
    assert candidates[3].relation_label == "固定队友"
    assert candidates[3].engagement_count == 3
    assert candidates[3].distinct_days == 3
    assert candidates[3].score == 100.0
    assert candidates[4].relation_label == "经常同行"
    assert candidates[4].score == 66.7
    assert candidates[5].relation_label == "固定队友"
    assert candidates[5].affiliation_label == "同军团"
    assert report.pilot_ships[0].id == 1001
    assert report.pilot_ships[0].kill_count == 3
    assert report.pilot_ships[0].loss_count == 0
    assert analyzer.top_associate_ids(mails, {1}) == [3, 4, 5]


def test_configured_friendlies_are_excluded_from_inferred_enemy_fleet(
    now, identities, ship_types
) -> None:
    mails = [
        _mail(
            200 + index,
            now - timedelta(days=index),
            [
                Participant(character_id=999 + index, is_victim=True),
                Participant(character_id=1, corporation_id=101, ship_type_id=1001),
                Participant(character_id=3, corporation_id=303, ship_type_id=1002),
                Participant(character_id=4, corporation_id=404, ship_type_id=1001),
                Participant(character_id=5, corporation_id=505, ship_type_id=1001),
                Participant(character_id=6, alliance_id=606, ship_type_id=1001),
            ],
        )
        for index in (1, 2)
    ]
    analyzer = FleetAnalyzer(
        friendly_character_ids={4},
        friendly_corporation_ids={505},
        friendly_alliance_ids={606},
    )

    report = analyzer.analyze(
        request_id="friendly-filter",
        requested_count=1,
        identities=identities[:1],
        invalid_names=[],
        killmails=mails,
        ship_types=ship_types,
        covered_character_ids={1},
        associate_names={3: "Unknown Wingmate", 4: "Blue Pilot", 5: "Green Pilot"},
        now=now,
    )

    assert report.latest_engagement is not None
    assert report.latest_engagement.fleet_size == 2
    assert [item.id for item in report.common_associates] == [3]
    assert analyzer.top_associate_ids(mails, {1}) == [3]


def test_latest_battle_skips_newer_loss_only_cluster(now, identities, ship_types) -> None:
    kill = _mail(
        401,
        now - timedelta(hours=2),
        [
            Participant(character_id=90, ship_type_id=1002, is_victim=True),
            Participant(character_id=1, ship_type_id=1001),
        ],
        value=2_500_000_000,
    )
    loss = _mail(
        402,
        now - timedelta(minutes=10),
        [
            Participant(character_id=1, ship_type_id=1001, is_victim=True),
            Participant(character_id=91, ship_type_id=1002),
        ],
        value=850_000_000,
    )

    report = FleetAnalyzer().analyze(
        request_id="latest-loss",
        requested_count=1,
        identities=identities[:1],
        invalid_names=[],
        killmails=[kill, loss],
        ship_types=ship_types,
        covered_character_ids={1},
        now=now,
    )

    assert report.latest_engagement is not None
    assert report.latest_engagement.outcome == "参与击毁"
    assert report.latest_engagement.result_detail == "主要目标 Logistics Cruiser"
    assert report.latest_engagement.total_value == 2_500_000_000
    assert report.latest_engagement.fleet_size == 1
    assert report.recent_engagements[0].outcome == "舰船损失"
    assert report.recent_engagements[0].lost_ships[0].id == 1001


def test_recent_battles_merge_nearby_cross_system_reports(
    now, identities, ship_types
) -> None:
    mails = [
        _mail(
            410,
            now - timedelta(minutes=25),
            [
                Participant(character_id=90, ship_type_id=1002, is_victim=True),
                Participant(character_id=1, ship_type_id=1001),
            ],
            system=30000142,
            value=100_000_000,
        ),
        _mail(
            411,
            now - timedelta(minutes=5),
            [
                Participant(character_id=91, ship_type_id=1002, is_victim=True),
                Participant(character_id=1, ship_type_id=1001),
            ],
            system=30000143,
            value=200_000_000,
        ),
    ]
    systems = {
        30000142: SolarSystemInfo(
            solar_system_id=30000142,
            name="YMJG-4",
            region_id=10000003,
            region_name="静寂谷",
        ),
        30000143: SolarSystemInfo(
            solar_system_id=30000143,
            name="DAYP-G",
            region_id=10000003,
            region_name="静寂谷",
        ),
    }

    report = FleetAnalyzer().analyze(
        request_id="cross-system-battle",
        requested_count=1,
        identities=identities[:1],
        invalid_names=[],
        killmails=mails,
        ship_types=ship_types,
        covered_character_ids={1},
        solar_systems=systems,
        now=now,
    )

    assert len(report.recent_engagements) == 1
    assert report.recent_engagements[0].system_name == "YMJG-4 / DAYP-G"
    assert report.recent_engagements[0].destroyed_value == 300_000_000


def test_latest_loss_keeps_combat_ship_instead_of_followup_capsule(
    now, identities, ship_types
) -> None:
    capsule_type_id = 670
    all_ship_types = dict(ship_types)
    all_ship_types[capsule_type_id] = ShipTypeInfo(
        type_id=capsule_type_id,
        name="太空舱",
        group_id=29,
        group_name="太空舱",
        group_name_en="Capsule",
        category_id=6,
        role=ShipRole.OTHER,
    )
    mails = [
        _mail(
            500,
            now - timedelta(minutes=15),
            [
                Participant(character_id=90, ship_type_id=1002, is_victim=True),
                Participant(character_id=1, ship_type_id=1001),
            ],
            value=250_000_000,
        ),
        _mail(
            499,
            now - timedelta(minutes=13),
            [
                Participant(character_id=92, ship_type_id=capsule_type_id, is_victim=True),
                Participant(character_id=1, ship_type_id=1001),
            ],
            value=10_000,
        ),
        _mail(
            501,
            now - timedelta(minutes=10),
            [
                Participant(character_id=1, ship_type_id=1001, is_victim=True),
                Participant(character_id=91, ship_type_id=1002),
            ],
            value=850_000_000,
        ),
        _mail(
            502,
            now - timedelta(minutes=5),
            [
                Participant(character_id=1, ship_type_id=capsule_type_id, is_victim=True),
                Participant(character_id=91, ship_type_id=1002),
            ],
            value=10_000,
        ),
    ]

    report = FleetAnalyzer().analyze(
        request_id="loss-with-capsule",
        requested_count=1,
        identities=identities[:1],
        invalid_names=[],
        killmails=mails,
        ship_types=all_ship_types,
        covered_character_ids={1},
        now=now,
    )

    assert report.latest_engagement is not None
    assert report.latest_engagement.outcome == "交火并有损失"
    assert report.latest_engagement.result_detail == (
        "击毁 Logistics Cruiser · 损失 Damage Cruiser"
    )
    assert [(item.id, item.count) for item in report.latest_engagement.ships] == [(1001, 1)]


def test_confidence_and_ninety_day_boundary(now, identities, ship_types) -> None:
    mails = [
        _mail(
            index,
            now - timedelta(days=1, minutes=index),
            [
                Participant(character_id=99, is_victim=True),
                Participant(character_id=1, ship_type_id=1001),
            ],
        )
        for index in range(1, 21)
    ]
    mails.append(
        _mail(
            50,
            now - timedelta(days=91),
            [
                Participant(character_id=98, is_victim=True),
                Participant(character_id=2, ship_type_id=1002),
            ],
        )
    )

    report = FleetAnalyzer().analyze(
        request_id="job-2",
        requested_count=2,
        identities=identities,
        invalid_names=[],
        killmails=mails,
        ship_types=ship_types,
        covered_character_ids={1, 2},
        now=now,
    )
    profiles = {profile.character_id: profile for profile in report.profiles}
    assert profiles[1].confidence.value == "高"
    assert profiles[2].event_count == 0
    assert profiles[2].confidence.value == "低"
    assert report.data_events == 20


def test_partial_coverage_adds_warning(now, identities, ship_types) -> None:
    report = FleetAnalyzer().analyze(
        request_id="job-3",
        requested_count=2,
        identities=identities,
        invalid_names=[],
        killmails=[],
        ship_types=ship_types,
        covered_character_ids={1},
        now=now,
    )
    assert report.coverage_ratio == 0.5
    assert any("1/2" in warning for warning in report.warnings)
    assert report.low_confidence_count == 2


def test_analysis_can_use_historical_window_when_recent_window_is_empty(
    now, identities, ship_types
) -> None:
    historical = _mail(
        990,
        now - timedelta(days=400),
        [
            Participant(character_id=9, ship_type_id=1002, is_victim=True),
            Participant(character_id=1, ship_type_id=1001),
        ],
    )

    recent_only = FleetAnalyzer().analyze(
        request_id="historical-default",
        requested_count=1,
        identities=identities[:1],
        invalid_names=[],
        killmails=[historical],
        ship_types=ship_types,
        covered_character_ids={1},
        now=now,
    )
    historical_report = FleetAnalyzer().analyze(
        request_id="historical-fallback",
        requested_count=1,
        identities=identities[:1],
        invalid_names=[],
        killmails=[historical],
        ship_types=ship_types,
        covered_character_ids={1},
        window_days=401,
        now=now,
    )

    assert recent_only.data_events == 0
    assert historical_report.data_events == 1
    assert historical_report.data_window_days == 401
    assert historical_report.pilot_ships[0].id == 1001


def test_threat_doctrine_location_and_heatmap_are_explainable(now) -> None:
    identity = CharacterIdentity(
        character_id=1,
        name="Enemy Pilot",
        corporation_id=101,
        corporation_name="Enemy Corp",
    )
    ship_types = {
        1: ShipTypeInfo(
            type_id=1,
            name="缪宁级",
            name_en="Muninn",
            group_id=358,
            group_name="重型突击巡洋舰",
            group_name_en="Heavy Assault Cruiser",
            category_id=6,
            role=ShipRole.DPS,
        ),
        2: ShipTypeInfo(
            type_id=2,
            name="曲剑级",
            name_en="Scimitar",
            group_id=832,
            group_name="后勤舰",
            group_name_en="Logistics",
            category_id=6,
            role=ShipRole.LOGISTICS,
        ),
        3: ShipTypeInfo(
            type_id=3,
            name="休津级",
            name_en="Huginn",
            group_id=833,
            group_name="战斗侦察舰",
            group_name_en="Combat Recon Ship",
            category_id=6,
            role=ShipRole.EWAR,
        ),
    }
    mails = []
    for index in range(12):
        pair_index = index % 6
        mails.append(
            _mail(
                index + 1,
                now - timedelta(days=index % 6, hours=12 - index % 3),
                    [
                        Participant(character_id=999, is_victim=True),
                        Participant(character_id=1, ship_type_id=1, final_blow=True),
                        Participant(character_id=10 + pair_index, ship_type_id=1),
                        Participant(character_id=70 + pair_index, ship_type_id=1),
                        Participant(character_id=30 + pair_index, ship_type_id=2),
                        Participant(character_id=50 + pair_index, ship_type_id=3),
                ],
                system=30000142,
            )
        )
    systems = {
        30000142: SolarSystemInfo(
            solar_system_id=30000142,
            name="吉他",
            region_id=10000002,
            region_name="伏尔戈",
        )
    }

    report = FleetAnalyzer().analyze(
        request_id="intel-rich",
        requested_count=1,
        identities=[identity],
        invalid_names=[],
        killmails=mails,
        ship_types=ship_types,
        covered_character_ids={1},
        solar_systems=systems,
        now=now,
    )

    assert report.doctrines[0].name == "缪宁舰队"
    assert report.doctrines[0].confidence >= 80
    assert report.doctrines[0].encounter_count == 6
    assert report.doctrines[0].sample_count == 6
    assert any("缪宁级 通常3 · 大场3" in item for item in report.doctrines[0].evidence)
    assert report.top_systems[0].name == "吉他"
    assert report.top_regions[0].name == "伏尔戈"
    assert report.recent_7d_kills == 12
    assert report.kill_efficiency == 1
    assert report.threat_score >= 50
    assert sum(sum(row) for row in report.activity_week_hours) > 0
    assert any(component.explanation for component in report.threat_components)
