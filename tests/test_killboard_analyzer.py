from app.killboard.analyzer import (
    activity_score_bonus,
    analyze_character_activity,
    analyze_group_activity,
    analyze_system_activity,
    group_activity_score_bonus,
)


def test_analyze_character_activity_counts_kills_losses_and_systems():
    rows = [
        {
            "killmail_id": 1,
            "killmail_time": "2026-06-30T10:00:00Z",
            "solar_system_id": 30002813,
            "victim": {"character_id": 999, "ship_type_id": 111},
            "attackers": [{"character_id": 123}],
        },
        {
            "killmail_id": 2,
            "killmail_time": "2026-06-30T11:00:00Z",
            "solar_system_id": 30002814,
            "victim": {"character_id": 123, "ship_type_id": 222},
            "attackers": [{"character_id": 456}],
        },
        {
            "killmail_id": 3,
            "killmail_time": "2026-06-30T12:00:00Z",
            "solar_system_id": 30002813,
            "victim": {"character_id": 888, "ship_type_id": 111},
            "attackers": [{"character_id": 123}],
        },
    ]

    activity = analyze_character_activity(123, rows, window="24h")

    assert activity.character_id == 123
    assert activity.window == "24h"
    assert activity.kills == 2
    assert activity.losses == 1
    assert activity.systems == [30002813, 30002814]
    assert activity.ship_type_ids == [111, 222]
    assert activity.latest_kill_at == "2026-06-30T12:00:00Z"


def test_activity_score_bonus():
    one = analyze_character_activity(
        123,
        [
            {
                "victim": {"character_id": 999},
                "attackers": [{"character_id": 123}],
            }
        ],
    )
    busy = analyze_character_activity(
        123,
        [
            {
                "victim": {"character_id": index + 1000},
                "attackers": [{"character_id": 123}],
            }
            for index in range(5)
        ],
    )
    quiet = analyze_character_activity(123, [])

    assert activity_score_bonus(quiet) == 0
    assert activity_score_bonus(one) == 10
    assert activity_score_bonus(busy) == 20


def test_analyze_system_activity_counts_matching_killmails():
    rows = [
        {
            "killmail_id": 1,
            "killmail_time": "2026-06-30T10:00:00Z",
            "solar_system_id": 30002813,
            "victim": {"character_id": 999, "ship_type_id": 111},
            "attackers": [
                {"character_id": 123, "ship_type_id": 222},
                {"character_id": 456},
            ],
        },
        {
            "killmail_id": 2,
            "killmail_time": "2026-06-30T11:00:00Z",
            "solar_system_id": 30002814,
            "victim": {"character_id": 888, "ship_type_id": 333},
            "attackers": [{"character_id": 777}],
        },
        {
            "killmail_id": 3,
            "killmail_time": "2026-06-30T12:00:00Z",
            "solar_system_id": 30002813,
            "victim": {"character_id": 555, "ship_type_id": 444},
            "attackers": [{"character_id": 123}],
        },
    ]

    activity = analyze_system_activity(30002813, rows, window="24h")

    assert activity.system_id == 30002813
    assert activity.window == "24h"
    assert activity.kills == 2
    assert activity.character_ids == [123, 456, 555, 999]
    assert activity.ship_type_ids == [111, 222, 444]
    assert activity.latest_kill_at == "2026-06-30T12:00:00Z"


def test_analyze_group_activity_counts_kills_losses_and_participants():
    rows = [
        {
            "killmail_id": 1,
            "killmail_time": "2026-06-30T10:00:00Z",
            "solar_system_id": 30002813,
            "victim": {
                "character_id": 999,
                "corporation_id": 777,
                "ship_type_id": 111,
            },
            "attackers": [
                {"character_id": 123, "corporation_id": 456, "ship_type_id": 222},
                {"character_id": 124, "corporation_id": 456},
            ],
        },
        {
            "killmail_id": 2,
            "killmail_time": "2026-06-30T11:00:00Z",
            "solar_system_id": 30002814,
            "victim": {
                "character_id": 125,
                "corporation_id": 456,
                "ship_type_id": 333,
            },
            "attackers": [
                {"character_id": 888, "corporation_id": 777, "ship_type_id": 444}
            ],
        },
        {
            "killmail_id": 3,
            "killmail_time": "2026-06-30T12:00:00Z",
            "solar_system_id": 30002815,
            "victim": {"character_id": 555, "corporation_id": 999},
            "attackers": [{"character_id": 556, "corporation_id": 999}],
        },
    ]

    activity = analyze_group_activity(456, rows, "corporation", window="7d")

    assert activity.entity_type == "corporation"
    assert activity.entity_id == 456
    assert activity.window == "7d"
    assert activity.kills == 1
    assert activity.losses == 1
    assert activity.systems == [30002813, 30002814]
    assert activity.character_ids == [123, 124, 125, 888, 999]
    assert activity.ship_type_ids == [111, 222, 333, 444]
    assert activity.latest_kill_at == "2026-06-30T11:00:00Z"
    assert activity.to_dict()["corporation_id"] == 456


def test_group_activity_score_bonus_is_conservative():
    quiet = analyze_group_activity(456, [], "corporation")
    active = analyze_group_activity(
        456,
        [
            {
                "victim": {"corporation_id": 777},
                "attackers": [{"corporation_id": 456}],
            }
        ],
        "corporation",
    )
    busy = analyze_group_activity(
        456,
        [
            {
                "victim": {"corporation_id": 1000 + index},
                "attackers": [{"corporation_id": 456}],
            }
            for index in range(10)
        ],
        "corporation",
    )

    assert group_activity_score_bonus(quiet) == 0
    assert group_activity_score_bonus(active) == 5
    assert group_activity_score_bonus(busy) == 15
