"""Official response lifetime survives archival and restart."""

from email.utils import formatdate

from app.esi.http_freshness import public_freshness
from app.esi.personnel_archive import AffiliationUpdate, IdentityUpdate
from app.esi.personnel_runtime import PersonnelRuntime
from tests.test_personnel_archive import archive_factory  # noqa: F401


def test_official_age_is_subtracted_not_renewed():
    metadata = {"started": 1198, "received": 1200, "headers": {
        "Date": formatdate(1000, usegmt=True), "Expires": formatdate(4600, usegmt=True), "Age": "500"}}
    assert public_freshness(metadata) == {"fetched_at": 1200, "expires_at": 4298}
    metadata["headers"]["Cache-Control"] = "max-age=100"
    assert public_freshness(metadata)["expires_at"] == 1200


def test_missing_or_invalid_headers_are_immediately_stale():
    assert public_freshness({"started": 1000, "received": 1001, "headers": {}})["expires_at"] == 1001


def test_expiry_survives_restart_and_does_not_change_affiliation_revision(archive_factory):  # noqa: F811
    archive = archive_factory()
    archive.save_identity(IdentityUpdate(1, "Pilot", 100, 100))
    archive.save_affiliation(AffiliationUpdate(1, 10, 101, expires_at=120))
    revision = archive.get_profiles([1])[1]["revision"]
    archive.save_affiliation(AffiliationUpdate(1, 10, 102, expires_at=125))
    assert archive.get_profiles([1])[1]["revision"] == revision
    reopened = archive_factory()
    runtime = PersonnelRuntime(reopened, object(), now=lambda: 130)
    runtime._remember(list(reopened.get_profiles([1]).values()))
    assert runtime.profile(1)["affiliation_trusted"] is False
    assert runtime.profile(1)["corporation_id"] == 10


def test_future_affiliation_is_not_trusted_until_observation_time(archive_factory):  # noqa: F811
    archive = archive_factory()
    archive.save_identity(IdentityUpdate(1, "Pilot", 100, 100))
    archive.save_affiliation(AffiliationUpdate(1, 10, 102, expires_at=3600))
    clock = [100]
    runtime = PersonnelRuntime(archive, object(), now=lambda: clock[0])
    runtime._remember(list(archive.get_profiles([1]).values()))
    assert runtime.profile(1)["affiliation_trusted"] is False
    clock[0] = 102
    assert runtime.profile(1)["affiliation_trusted"] is True
