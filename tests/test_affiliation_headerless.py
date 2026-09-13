"""Official POST affiliation TTL fallback is route-specific and age-aware."""

from email.utils import formatdate

import pytest

from app.esi.http_freshness import public_freshness
from app.esi.transport import EsiConnections, TransportEsiClient
from tests.test_esi_transport import Connection, Response
from tests.test_personnel_archive import archive_factory  # noqa: F401


def metadata(path="/latest/characters/affiliation/", method="POST", **headers):
    return {
        "started": 1198,
        "received": 1200,
        "path": path,
        "method": method,
        "headers": {"Date": formatdate(1000, usegmt=True), **headers},
    }


def test_affiliation_uses_official_ttl_without_http_cache_headers():
    assert public_freshness(metadata()) == {"fetched_at": 1200, "expires_at": 4600}
    assert public_freshness(metadata(Age="500"))["expires_at"] == 4298
    assert public_freshness(metadata("/characters/affiliation"))["expires_at"] == 4600


@pytest.mark.parametrize(
    "headers",
    [
        {"Cache-Control": "no-store"},
        {"Cache-Control": "no-cache"},
        {"Cache-Control": "max-age=0"},
        {"Cache-Control": "max-age=bad"},
        {"Expires": "invalid"},
        {"Age": "invalid"},
        {"Date": "invalid"},
    ],
)
def test_explicit_invalid_or_restrictive_headers_never_get_fallback(headers):
    assert public_freshness(metadata(**headers))["expires_at"] == 1200


@pytest.mark.parametrize(
    "path,method",
    [
        ("/latest/characters/1/", "GET"),
        ("/latest/characters/affiliation/", "GET"),
        ("/latest/characters/1/contacts/", "GET"),
    ],
)
def test_other_routes_still_require_http_freshness(path, method):
    assert public_freshness(metadata(path, method))["expires_at"] == 1200


def test_official_fallback_does_not_extend_existing_deadlines_or_age():
    assert public_freshness(metadata(), maximum=300)["expires_at"] == 1300
    assert public_freshness(metadata(Age="3600"))["expires_at"] == 1200
    assert (
        public_freshness(metadata(**{"Cache-Control": "max-age=300"}))["expires_at"]
        == 1300
    )
    assert (
        public_freshness(metadata(Expires=formatdate(1500, usegmt=True)))["expires_at"]
        == 1500
    )


def test_transport_passes_route_metadata_and_archive_restores_trust(
    monkeypatch, archive_factory,  # noqa: F811
):
    from app.esi.personnel_archive import IdentityUpdate
    from app.esi.personnel_runtime import PersonnelRuntime

    pool = EsiConnections(size=1, rate=100000)
    connection = Connection(
        [
            Response(
                b'[{"character_id":1,"corporation_id":10}]',
                Date=formatdate(1000, usegmt=True),
            )
        ]
    )
    monkeypatch.setattr(pool, "_connection", lambda timeout: connection)
    monkeypatch.setattr("app.esi.transport.time.time", lambda: 1001)
    client = TransportEsiClient(pool)
    archive = archive_factory()
    archive.save_identity(IdentityUpdate(1, "Pilot", 100, 100))
    archive.request_refresh("affiliation", 1, priority=1, due_at=100)
    runtime = PersonnelRuntime(archive, client, now=lambda: 1001)
    runtime.run_batch("affiliation", realtime=True)
    assert runtime.profile(1)["affiliation_trusted"] is True
    assert runtime.profile(1)["affiliation_expires_at"] == 4600
    assert archive.claim(now=1002, kind="affiliation") == []
