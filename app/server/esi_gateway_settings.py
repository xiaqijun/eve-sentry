"""Store tested ESI gateway overrides in the server's private state directory."""

from __future__ import annotations

import http.client
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener
from uuid import uuid4

from app.esi.client import EsiApiError
from app.esi.remote import RemoteEsiClient
from app.esi.transport import EsiConnections, TransportEsiClient
from app.server.auth import AuthError

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class GatewaySettings:
    url: str
    token: str = field(repr=False)
    revision: str = "environment"


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _origin(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("网关地址无效")
    value = value.strip()
    if any(ord(char) <= 32 or ord(char) >= 127 or char in "\\%?#" for char in value):
        raise ValueError("网关地址无效")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("网关地址无效") from None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
            or parsed.path not in {"", "/"} or port == 0):
        raise ValueError("网关地址须为 HTTP 或 HTTPS 地址")
    return f"{parsed.scheme}://{parsed.netloc.lower()}"


class EsiGatewaySettings:
    def __init__(self, args, *, environment=None):
        env = os.environ if environment is None else environment
        mode = env.get("EVE_SENTRY_ESI_TRANSPORT", "legacy").strip()
        self.mode = "remote" if mode == "legacy" and args.esi_backend == "remote" else mode
        self.path = Path(args.config).with_suffix(".esi-gateway.json")
        self.values = GatewaySettings(args.esi_gateway_url.strip().rstrip("/"), args.esi_gateway_token.strip())
        self.client = None
        self.lock = threading.Lock()
        self.next_test = 0.0
        self.allowed = {_origin(value) for value in
                        env.get("EVE_SENTRY_SERVER_ESI_GATEWAY_ALLOWED_URLS", "").split(",") if value.strip()}
        if self.values.url:
            try:
                self.allowed.add(_origin(self.values.url))
            except ValueError:
                # Leave deployments with a legacy origin format on their startup configuration.
                pass

    def load(self, args):
        if self.mode not in {"relay", "remote"}:
            return
        try:
            with self.path.open(encoding="utf-8") as file:
                if os.name != "nt" and os.fstat(file.fileno()).st_mode & 0o077:
                    raise ValueError("gateway settings are not private")
                document = json.load(file)
            values = GatewaySettings(**document)
            if not isinstance(values.revision, str) or not values.revision or values.revision == "environment":
                raise ValueError("invalid revision")
            self._candidate(values)
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError):
            LOGGER.warning("Ignoring unavailable or invalid ESI gateway override; using startup settings")
            return
        self.values = values
        args.esi_gateway_url, args.esi_gateway_token = values.url, values.token

    def bind(self, client):
        self.client = client
        if self.values.revision != "environment" and isinstance(client, RemoteEsiClient):
            client.set_gateway(self.values.url, self.values.token, self._opener())

    def snapshot(self):
        settings = self.values
        try:
            visible_url = _origin(settings.url) if settings.url else ""
        except ValueError:
            visible_url = ""
        return {
            "url": visible_url,
            "token_configured": bool(settings.token),
            "revision": settings.revision,
            "allowed_urls": sorted(self.allowed),
            "editable": bool(self.allowed) and (
                self.mode == "relay" and isinstance(self.client, TransportEsiClient)
                or self.mode == "remote" and isinstance(self.client, RemoteEsiClient)
            ),
        }

    @staticmethod
    def _opener():
        return build_opener(ProxyHandler({}), _RejectRedirects()).open

    def _candidate(self, settings):
        if _origin(settings.url) not in self.allowed:
            raise ValueError("该地址未列入服务器允许的网关地址")
        if (not isinstance(settings.token, str) or not 32 <= len(settings.token) <= 512
                or any(not 33 <= ord(char) <= 126 for char in settings.token)):
            raise ValueError("服务令牌须为 32～512 个可见 ASCII 字符")
        if self.mode == "relay":
            url = urlsplit(settings.url)
            if url.scheme != "http":
                raise ValueError("Relay 网关须使用私网 HTTP 地址")
            try:
                return EsiConnections(relay_host=url.hostname, relay_port=url.port or 8787, relay_token=settings.token)
            except ValueError:
                raise ValueError("Relay 网关须使用私网 IP 地址") from None
        return RemoteEsiClient(settings.url, settings.token, timeout=5, opener=self._opener())

    def _verify(self, candidate):
        now = time.monotonic()
        if now < self.next_test:
            raise AuthError("请稍后再测试连接", 429, "esi_gateway_test_rate_limited")
        self.next_test = now + 2
        try:
            if isinstance(candidate, EsiConnections):
                candidate.test_relay()
            elif candidate.get_system(30000142).get("system_id") != 30000142:
                raise ValueError("invalid gateway system response")
        except (OSError, ValueError, AttributeError, http.client.HTTPException, EsiApiError):
            raise AuthError("ESI 网关连接或鉴权失败", 502, "esi_gateway_check_failed") from None

    def _require_editable(self):
        if not self.snapshot()["editable"]:
            raise AuthError("当前部署未启用可配置的 ESI 网关", 409, "esi_gateway_unavailable")

    def test(self):
        with self.lock:
            self._require_editable()
            self._verify(self._candidate(self.values))

    def update(self, payload):
        if (not isinstance(payload, dict) or set(payload) - {"url", "revision", "token"}
                or not {"url", "revision"} <= set(payload)):
            raise ValueError("请提交网关地址和配置版本")
        with self.lock:
            self._require_editable()
            current = self.values
            if payload["revision"] != current.revision:
                raise AuthError("配置已更新，请刷新后重试", 409, "settings_conflict")
            url = _origin(payload["url"])
            token = payload.get("token", "")
            if not isinstance(token, str):
                raise ValueError("服务令牌无效")
            if not token and url == self.snapshot()["url"]:
                token = current.token
            values = GatewaySettings(url, token, uuid4().hex)
            candidate = self._candidate(values)
            self._verify(candidate)
            if (url, token) == (current.url, current.token):
                return self.snapshot()
            self._write(values)
            if isinstance(candidate, EsiConnections):
                self.client.connections.set_relay(candidate.relay_target)
            else:
                self.client.use_gateway(candidate)
            self.values = values
            return self.snapshot()

    def _write(self, values):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=self.path.name + ".", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as file:
                    json.dump({"url": values.url, "token": values.token, "revision": values.revision}, file)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(name, self.path)
            finally:
                Path(name).unlink(missing_ok=True)
        except OSError:
            raise AuthError("ESI 网关配置保存失败", 503, "esi_gateway_storage_error") from None
