from app.killboard.analyzer import analyze_character_activity, activity_score_bonus


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

