"""Operational errors retain useful classifications without private messages."""

from urllib.error import HTTPError, URLError

from app.esi.contact_http import ContactReadError
from app.esi.diagnostics import failure_summary
from tests.test_personnel_archive import archive_factory  # noqa: F401
from tests.test_relation_archive import archive  # noqa: F401
from tests.test_relation_refresh import setup  # noqa: F401


def test_safe_contact_cause_status_and_sqlstate():
    error = ContactReadError("contacts_read_failed", status=503)
    error.__cause__ = URLError(ConnectionResetError("secret token"))
    assert failure_summary(error) == (
        "error=ContactReadError reason=contacts_read_failed status=503 "
        "cause=URLError cause=ConnectionResetError"
    )
    database_error = RuntimeError("postgresql://user:secret@host/database")
    database_error.sqlstate = "40P01"
    assert failure_summary(database_error) == "error=RuntimeError sqlstate=40P01"


def test_untrusted_messages_urls_and_cycles_are_not_logged():
    error = ContactReadError("Bearer secret\nforged_log")
    error.__cause__ = HTTPError(
        "https://private/?token=secret", 403, "private body", {}, None
    )
    error.__cause__.__cause__ = error
    assert failure_summary(error) == "error=ContactReadError cause=HTTPError status=403"


def test_refresh_failure_logs_safe_cause_and_retains_retry(archive_factory, caplog):  # noqa: F811
    from app.esi.personnel_archive import IdentityUpdate
    from app.esi.personnel_runtime import PersonnelRuntime

    class Client:
        def get_character_affiliations(self, keys):
            raise URLError(ConnectionResetError("private URL token=secret"))

    personnel_archive = archive_factory()
    personnel_archive.save_identity(IdentityUpdate(1, "Pilot", 100, 100))
    personnel_archive.request_refresh("affiliation", 1, priority=1, due_at=100)
    runtime = PersonnelRuntime(personnel_archive, Client(), now=lambda: 101)
    assert runtime.run_batch("affiliation", realtime=True) == 1
    assert "cause=ConnectionResetError" in caplog.text
    assert "secret" not in caplog.text
    assert personnel_archive.get_profiles([1])[1]["name"] == "Pilot"
    assert runtime.snapshot()["refresh_errors"] == 1


def test_scheduler_failure_logs_sqlstate_without_sql_or_credentials(
    monkeypatch, caplog
):
    from app.esi.personnel_runtime import PersonnelRuntime

    runtime = PersonnelRuntime(None, None, now=lambda: 101)

    def fail():
        runtime.request_stop()
        error = RuntimeError("SQL contains private credential")
        error.sqlstate = "40P01"
        raise error

    monkeypatch.setattr(runtime, "drain_requests", fail)
    monkeypatch.setattr(runtime._wake, "wait", lambda *_: None)
    runtime._run()
    assert "Personnel scheduler degraded" in caplog.text
    assert "sqlstate=40P01" in caplog.text
    assert "credential" not in caplog.text


def test_organization_failure_logs_expiry_reason_and_preserves_source(
    setup, monkeypatch, caplog,  # noqa: F811
):
    from app.esi.contact_http import ContactRows

    clock, client, _, manager = setup
    manager.refresh()
    clock[0] = 1127
    monkeypatch.setattr(
        client, "get_corporation_contacts", lambda *_: ContactRows([], expires_at=1000)
    )
    manager.refresh()
    assert "reason=organization_contacts_expiry_unavailable" in caplog.text
    source = manager.view().sources[0]
    assert source.failures == 1
    assert source.entries[("corporation", 30)] == 5
