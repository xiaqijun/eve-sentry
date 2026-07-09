import pytest

from app.server.postgres_store import PostgreSQLIntelStore, _redact_dsn


def test_postgres_store_requires_dsn():
    with pytest.raises(ValueError, match="postgres dsn is required"):
        PostgreSQLIntelStore("")


def test_postgres_dsn_redaction_hides_credentials():
    redacted = _redact_dsn(
        "postgresql://eve_sentry:super-secret@db.internal:5432/eve_sentry"
    )

    assert redacted == "postgresql://***@db.internal:5432/eve_sentry"
    assert "super-secret" not in redacted
