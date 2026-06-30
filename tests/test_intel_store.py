from app.core.models import Evidence, ThreatEvent
from app.intel.enrichment import ThreatEnrichment
from app.intel.scoring import ScoringEngine, Watchlist
from app.killboard.analyzer import KillActivity
from app.server.intel_store import IntelStore, StarSystem


def test_add_report_persists_and_snapshot_aggregates(tmp_path):
    path = tmp_path / "intel_reports.json"
    store = IntelStore(
        path,
        systems={"Tama": StarSystem("Tama", 10, 20, "The Citadel", 0.3)},
        links=[],
    )

    report = store.add_report(
        system=" Tama ",
        names=[" Alice ", "Bob", "Alice"],
        source="test",
        seen_at="2026-06-29T12:00:00+00:00",
    )

    assert report.system == "Tama"
    assert report.names == ["Alice", "Bob"]

    reloaded = IntelStore(path, systems={}, links=[])
    snapshot = reloaded.snapshot()

    assert snapshot["summary"]["report_count"] == 1
    assert snapshot["summary"]["observation_count"] == 1
    assert snapshot["summary"]["alert_count"] == 1
    assert snapshot["summary"]["hostile_count"] == 2
    assert snapshot["systems"][0]["name"] == "Tama"
    assert snapshot["systems"][0]["hostiles"] == ["Alice", "Bob"]
    assert snapshot["observations"][0]["system_name"] == "Tama"
    assert snapshot["alerts"][0]["level"] == "low"


def test_list_reports_filters_and_limits(tmp_path):
    store = IntelStore(tmp_path / "intel_reports.json", systems={}, links=[])
    store.add_report("Tama", ["Alice"], seen_at="2026-06-29T12:00:00+00:00")
    store.add_report("Jita", ["Bob"], seen_at="2026-06-29T12:01:00+00:00")
    store.add_report("Tama", ["Carol"], seen_at="2026-06-29T12:02:00+00:00")

    assert [r["names"][0] for r in store.list_reports(limit=2)] == ["Carol", "Bob"]
    assert [r["names"][0] for r in store.list_reports(system="tama")] == [
        "Carol",
        "Alice",
    ]
    assert [r["system"] for r in store.list_reports(name="bob")] == ["Jita"]


def test_delete_report_removes_and_persists(tmp_path):
    path = tmp_path / "intel_reports.json"
    store = IntelStore(path, systems={}, links=[])
    report = store.add_report("Tama", ["Alice"])

    assert store.delete_report(report.report_id) is True
    assert store.delete_report(report.report_id) is False
    assert IntelStore(path, systems={}, links=[]).snapshot()["summary"][
        "report_count"
    ] == 0


def test_add_observation_persists_and_lists_alerts(tmp_path):
    path = tmp_path / "intel_reports.json"
    store = IntelStore(path, systems={}, links=[])

    observation = store.add_observation(
        {
            "source": "intel_channel",
            "source_instance": "Alliance Intel",
            "system_name": " Tama ",
            "names": [" Alice ", "Alice"],
            "raw_text": "Tama Alice",
            "seen_at": "2026-06-29T12:00:00+00:00",
            "received_at": "2026-06-29T12:00:01+00:00",
        }
    )

    assert observation.system_name == "Tama"
    assert observation.names == ["Alice"]

    reloaded = IntelStore(path, systems={}, links=[])
    observations = reloaded.list_observations(source="intel_channel")
    alerts = reloaded.list_alerts()

    assert observations[0]["raw_text"] == "Tama Alice"
    assert alerts[0]["id"] == f"evt_{observation.observation_id}"
    assert alerts[0]["score"] == 30
    assert alerts[0]["evidence"][0]["type"] == "intel_channel_observed"


def test_add_observation_uses_optional_resolver(tmp_path):
    class FakeResolver:
        def enrich_observation(self, observation):
            observation.system_id = 30002813
            observation.character_ids = [123]
            return observation

    store = IntelStore(
        tmp_path / "intel_reports.json",
        systems={},
        links=[],
        resolver=FakeResolver(),
    )

    observation = store.add_observation(
        {
            "source": "intel_channel",
            "system_name": "Tama",
            "names": ["Alice"],
        }
    )

    assert observation.system_id == 30002813
    assert observation.character_ids == [123]
    assert store.list_observations()[0]["character_ids"] == [123]


def test_list_alerts_uses_optional_scorer_and_caches_result(tmp_path):
    class FakeScorer:
        def __init__(self):
            self.calls = 0

        def score(self, observation):
            self.calls += 1
            if self.calls > 1:
                return None
            return ThreatEvent(
                event_id=f"custom_{observation.observation_id}",
                system_name=observation.system_name,
                names=observation.names,
                score=99,
                level="high",
                evidence=[Evidence("custom", 99, "custom score")],
                source_observation_id=observation.observation_id,
                created_at=observation.received_at,
            )

    scorer = FakeScorer()
    store = IntelStore(
        tmp_path / "intel_reports.json",
        systems={},
        links=[],
        scorer=scorer,
    )
    observation = store.add_observation(
        {
            "source": "intel_channel",
            "system_name": "Tama",
            "names": ["Alice"],
            "received_at": "2026-06-29T12:00:01+00:00",
        }
    )

    first_alerts = store.list_alerts()
    second_alerts = store.list_alerts()

    assert first_alerts == second_alerts
    assert first_alerts[0]["id"] == f"custom_{observation.observation_id}"
    assert first_alerts[0]["score"] == 99
    assert first_alerts[0]["evidence"][0]["type"] == "custom"
    assert scorer.calls == 1


def test_list_alerts_scores_with_optional_enricher(tmp_path):
    class FakeEnricher:
        def enrich(self, observation):
            return ThreatEnrichment(
                character_profiles=[
                    {"character_id": observation.character_ids[0], "corporation_id": 42}
                ],
                kill_activities=[
                    KillActivity(
                        character_id=observation.character_ids[0],
                        window="7d",
                        kills=5,
                    )
                ],
            )

    store = IntelStore(
        tmp_path / "intel_reports.json",
        systems={},
        links=[],
        scorer=ScoringEngine(
            watchlist=Watchlist(hostile_corporation_ids={42}),
            cooldown_seconds=0,
        ),
        enricher=FakeEnricher(),
    )
    store.add_observation(
        {
            "source": "intel_channel",
            "system_name": "Tama",
            "names": ["Alice"],
            "character_ids": [123],
            "received_at": "2026-06-29T12:00:01+00:00",
        }
    )

    alert = store.list_alerts()[0]

    assert alert["score"] == 110
    assert alert["level"] == "critical"
    assert [item["type"] for item in alert["evidence"]] == [
        "intel_channel_report",
        "hostile_corporation",
        "recent_kill_activity",
    ]


def test_enricher_failure_falls_back_to_base_scoring(tmp_path):
    class FailingEnricher:
        def enrich(self, observation):
            raise RuntimeError("offline")

    store = IntelStore(
        tmp_path / "intel_reports.json",
        systems={},
        links=[],
        scorer=ScoringEngine(cooldown_seconds=0),
        enricher=FailingEnricher(),
    )
    store.add_observation(
        {
            "source": "intel_channel",
            "system_name": "Tama",
            "names": ["Alice"],
            "character_ids": [123],
        }
    )

    alert = store.list_alerts()[0]

    assert alert["score"] == 30
    assert [item["type"] for item in alert["evidence"]] == [
        "intel_channel_report"
    ]
