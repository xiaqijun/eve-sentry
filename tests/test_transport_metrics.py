from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from app.esi.transport import EsiConnections
from tests.test_esi_transport import Connection, Response


def test_attempts_and_local_throttle_are_distinct_without_token_logging(
    monkeypatch, caplog
):
    pool = EsiConnections(size=1)
    connection = Connection([Response(status=429)])
    monkeypatch.setattr(pool, "_connection", lambda timeout: connection)
    request = Request(
        "https://esi.evetech.net/latest/corporations/123/contacts/",
        headers={"Authorization": "Bearer private-token"},
    )
    with caplog.at_level("INFO"):
        with pytest.raises(HTTPError):
            pool(request, timeout=1)
        with pytest.raises(URLError):
            pool(request, timeout=1)
    stats = pool.telemetry.snapshot()
    assert (stats["requests"], stats["attempts"], stats["final_errors"]) == (2, 1, 2)
    assert stats["first_attempt_errors"] == 1
    assert stats["budget_or_validation_errors"] == 1
    assert stats["fallbacks"] == stats["retries"] == 0
    assert "private-token" not in caplog.text and "123" not in caplog.text
