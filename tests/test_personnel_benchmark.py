"""Safety and payload checks for the local-only refresh benchmark."""

from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("psycopg_pool")

from scripts.benchmark_personnel_refresh import FakeEsi, distribution, validate_local_dsn


@pytest.mark.parametrize("dsn", [
    "host=114.132.167.239 port=55440 user=codex_test",
    "host=127.0.0.1 port=5432 user=codex_test",
    "host=127.0.0.1 port=55440 user=postgres",
    "host=127.0.0.1 hostaddr=47.243.104.165 port=55440 user=codex_test",
    "host=127.0.0.1 port=55440 user=codex_test service=production",
    "host=127.0.0.1 port=0 user=codex_test",
    "",
])
def test_benchmark_rejects_unsafe_targets(dsn):
    with pytest.raises(ValueError):
        validate_local_dsn(dsn)


def test_benchmark_allows_explicit_local_test_target():
    validate_local_dsn("host=127.0.0.1 port=55440 user=codex_test dbname=postgres")


def test_benchmark_fake_affiliation_matches_seed_and_keeps_thread_freshness():
    client = FakeEsi(0)

    def request(cid):
        rows = client.get_character_affiliations([cid])
        metadata = client.response_freshness()
        assert set(metadata) == {str(cid)}
        assert metadata[str(cid)]["expires_at"] - metadata[str(cid)]["fetched_at"] == 3600
        return rows[0]

    with ThreadPoolExecutor(max_workers=2) as executor:
        rows = list(executor.map(request, [900000003, 900001015]))
    assert rows == [
        {"character_id": 900000003, "corporation_id": 98000003, "alliance_id": 99000003},
        {"character_id": 900001015, "corporation_id": 98000015, "alliance_id": 99000015},
    ]
    assert client.batch_sizes == [1, 1]


def test_benchmark_distribution_labels_samples():
    assert distribution([]) == {"count": 0}
    assert distribution([3, 1, 2]) == {"count": 3, "median": 2, "p95": 3, "max": 3}
