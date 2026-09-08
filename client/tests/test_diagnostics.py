import logging

from app.diagnostics import configure_client_logging


def test_configure_client_logging_suppresses_successful_http_request_noise(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    previous_levels = (httpx_logger.level, httpcore_logger.level)
    try:
        configure_client_logging(logging.INFO)
        assert httpx_logger.level == logging.WARNING
        assert httpcore_logger.level == logging.WARNING
    finally:
        httpx_logger.setLevel(previous_levels[0])
        httpcore_logger.setLevel(previous_levels[1])
