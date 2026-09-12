"""TLS relay client contract without contacting any external service."""

import io
import ssl
from email.message import Message
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from app.esi.transport import EsiConnections, TransportEsiClient, configured_client


class Response:
    def __init__(self, body=b"{}", status=200, **headers):
        self.stream = io.BytesIO(body)
        self.status, self.will_close = status, False
        self.headers = Message()
        for key, value in headers.items():
            self.headers[key] = value

    def read1(self, size):
        return self.stream.read(size)


class Connection:
    sock = None

    def __init__(self, responses):
        self.responses, self.requests, self.closed = responses, [], False

    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))

    def getresponse(self):
        return self.responses.pop(0)

    def close(self):
        self.closed = True


def test_pool_reuses_fully_read_responses_and_preserves_304(monkeypatch):
    pool = EsiConnections(size=1, rate=100000)
    connection = Connection([Response(b'{"name":"Pilot"}'), Response(status=304, ETag='"v1"')])
    created = []
    monkeypatch.setattr(pool, "_connection", lambda timeout: created.append(timeout) or connection)
    client = TransportEsiClient(pool)
    assert client.get_character(1) == {"name": "Pilot"}
    with pytest.raises(HTTPError) as exc:
        pool(Request("https://esi.evetech.net/latest/characters/1/"), timeout=1)
    assert exc.value.code == 304
    assert exc.value.headers["ETag"] == '"v1"'
    assert len(created) == 1 and not connection.closed


def test_throttle_blocks_all_subsequent_requests_without_exit_fallback(monkeypatch):
    pool = EsiConnections(size=1)
    connection = Connection([Response(status=429, **{"Retry-After": "900"})])
    monkeypatch.setattr(pool, "_connection", lambda timeout: connection)
    request = Request("https://esi.evetech.net/latest/characters/1/")
    with pytest.raises(HTTPError) as exc:
        pool(request, timeout=1)
    assert exc.value.headers["Retry-After"] == "900"
    with pytest.raises(URLError, match="budget unavailable"):
        pool(request, timeout=1)
    assert len(connection.requests) == 1


@pytest.mark.parametrize("url", ["http://esi.evetech.net/", "https://evil.test/", "https://esi.evetech.net:444/", "https://x@esi.evetech.net/"])
def test_only_official_tls_target_is_allowed(url):
    with pytest.raises(URLError, match="not allowed"):
        EsiConnections()(Request(url), timeout=1)


def test_tls_tunnel_verifies_official_host_and_keeps_credentials_separate(monkeypatch):
    pool = EsiConnections(relay_host="10.1.2.3", relay_token="g" * 32)
    connection = pool._connection(1)
    assert connection.host == "10.1.2.3"
    assert connection._tunnel_host == "esi.evetech.net"
    assert connection._tunnel_headers["Authorization"] == "Bearer " + "g" * 32
    assert pool.context.check_hostname and pool.context.verify_mode == ssl.CERT_REQUIRED
    fake = Connection([Response()])
    monkeypatch.setattr(pool, "_connection", lambda timeout: fake)
    TransportEsiClient(pool).get_character_location(1, "private-token")
    headers = fake.requests[0][1]["headers"]
    assert headers["Authorization"] == "Bearer private-token"
    assert "g" * 32 not in str(fake.requests)


def test_transport_failure_returns_slot_without_retry(monkeypatch):
    pool = EsiConnections(size=1)
    connection = Connection([])
    monkeypatch.setattr(pool, "_connection", lambda timeout: connection)
    monkeypatch.setattr(connection, "getresponse", lambda: (_ for _ in ()).throw(OSError("private")))
    with pytest.raises(URLError, match="transport failed"):
        pool(Request("https://esi.evetech.net/latest/characters/1/"), timeout=1)
    assert connection.closed and pool.pool.qsize() == 1
    assert len(connection.requests) == 1


def test_legacy_default_and_explicit_relay_configuration(monkeypatch):
    monkeypatch.delenv("EVE_SENTRY_ESI_TRANSPORT", raising=False)
    args = SimpleNamespace(esi_gateway_url="http://10.1.2.3:8787", esi_gateway_token="g" * 32)
    assert configured_client(args) is None
    monkeypatch.setenv("EVE_SENTRY_ESI_TRANSPORT", "relay")
    first, second = configured_client(args), configured_client(args)
    assert first.connections is second.connections


@pytest.mark.parametrize("url", ["http://", "http:///", "http://8.8.8.8", "http://0.0.0.0", "https://10.1.2.3"])
def test_bad_relay_configuration_never_silently_becomes_direct(monkeypatch, url):
    monkeypatch.setenv("EVE_SENTRY_ESI_TRANSPORT", "relay")
    with pytest.raises(ValueError):
        configured_client(SimpleNamespace(esi_gateway_url=url, esi_gateway_token="g" * 32))


def test_untrusted_tls_certificate_is_rejected(tmp_path, monkeypatch):
    import datetime
    import http.client
    import socketserver
    import threading

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    from app.esi.client import EsiApiError

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "esi.evetech.net")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(minutes=5)).sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "test-cert.pem", tmp_path / "test-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                with context.wrap_socket(self.request, server_side=True):
                    pass
            except ssl.SSLError:
                pass  # The client must reject this intentionally untrusted certificate.

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    pool = EsiConnections(size=1)
    monkeypatch.setattr(pool, "_connection", lambda timeout: http.client.HTTPSConnection(
        "127.0.0.1", server.server_address[1], timeout=timeout, context=pool.context))
    try:
        with pytest.raises(EsiApiError) as error:
            TransportEsiClient(pool).get_character(1)
        assert isinstance(error.value.__cause__.__cause__, ssl.SSLCertVerificationError)
        assert pool.pool.qsize() == 1
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)
