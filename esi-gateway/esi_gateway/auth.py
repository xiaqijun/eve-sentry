"""Bearer-token and source-address checks for protected routes."""

from __future__ import annotations

import hmac


class Authorizer:
    def __init__(self, token: str, allowed_clients: set[str] | None = None) -> None:
        self.token = str(token).strip()
        self.allowed_clients = set(allowed_clients or ())

    def check(self, peer: str, authorization: str) -> str | None:
        if self.allowed_clients and peer not in self.allowed_clients:
            return "source_not_allowed"
        if not hmac.compare_digest(str(authorization or ""), f"Bearer {self.token}"):
            return "unauthorized"
        return None
