import io
import json

from app.core.models import Observation
from app.intel.enrichment import ThreatEnricher
from app.intel.zkillboard import ZkillboardClient


class FakeResponse:
    def __init__(self, payload):
        self._stream = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        return self._stream.read(size)


def test_character_stats_normalizes_and_caches_zkillboard_payload():
    calls = []

    def opener(request, timeout):
        calls.append((request, timeout))
        return FakeResponse(
            {
                "dangerRatio": 86,
                "gangRatio": 72,
                "soloRatio": 28,
                "shipsDestroyed": 2628,
                "shipsLost": 246,
                "iskDestroyed": 2601278340937,
                "iskLost": 26517101560,
            }
        )

    client = ZkillboardClient(
        opener=opener,
        min_request_interval_seconds=0,
    )

    first = client.character_stats(93541545)
    second = client.character_stats(93541545)

    assert first is not None
    assert first["danger_ratio"] == 86
    assert first["gang_ratio"] == 72
    assert first["ships_destroyed"] == 2628
    assert first["source"] == "zkillboard"
    assert first["source_url"].endswith("/stats/characterID/93541545/")
    assert second == first
    assert len(calls) == 1
    assert calls[0][0].headers["User-agent"].startswith("eve-sentry/")
    assert calls[0][1] == 4.0


def test_character_stats_negative_caches_empty_and_failed_responses():
    calls = 0

    def opener(_request, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return FakeResponse({"type": "characterID", "id": 123})
        raise OSError("offline")

    client = ZkillboardClient(
        opener=opener,
        min_request_interval_seconds=0,
    )

    assert client.character_stats(123) is None
    assert client.character_stats(123) is None
    assert calls == 1


def test_threat_enricher_adds_zkill_stats_without_changing_profile_fields():
    class Resolver:
        def character_profile(self, character_id):
            return {
                "character_id": character_id,
                "name": "Pilot One",
                "corporation_id": 98000001,
            }

    class Killboard:
        def character_stats(self, character_id):
            return {
                "source": "zkillboard",
                "character_id": character_id,
                "danger_ratio": 68,
                "ships_destroyed": 1043,
            }

    observation = Observation.from_payload(
        {
            "source": "eve-sentry-detector",
            "system_name": "Jita",
            "character_ids": [443630591],
        }
    )
    enricher = ThreatEnricher(resolver=Resolver(), killboard=Killboard())

    enrichment = enricher.enrich(observation)

    assert enrichment.character_profiles == [
        {
            "character_id": 443630591,
            "name": "Pilot One",
            "corporation_id": 98000001,
            "zkill": {
                "source": "zkillboard",
                "character_id": 443630591,
                "danger_ratio": 68,
                "ships_destroyed": 1043,
            },
            "zkill_danger_ratio": 68,
        }
    ]
