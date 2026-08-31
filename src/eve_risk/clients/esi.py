from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Iterable
from datetime import datetime

import httpx

from eve_risk.clients.base import ExternalServiceError, request_with_retries
from eve_risk.domain import CharacterIdentity, ShipTypeInfo
from eve_risk.sde import SDELocalization
from eve_risk.ship_roles import ShipRoleClassifier


class ESIClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        classifier: ShipRoleClassifier,
        sde: SDELocalization | None = None,
        concurrency: int = 10,
        entity_cache_ttl_seconds: int = 21600,
        entity_cache_entries: int = 4096,
    ) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.classifier = classifier
        self.sde = sde
        self.semaphore = asyncio.Semaphore(concurrency)
        self.entity_cache_ttl_seconds = max(1, int(entity_cache_ttl_seconds))
        self.entity_cache_entries = max(16, int(entity_cache_entries))
        self._type_cache: dict[int, ShipTypeInfo] = {}
        self._entity_cache: OrderedDict[tuple[str, int], tuple[float, object]] = OrderedDict()

    async def resolve_characters(
        self, names: list[str]
    ) -> tuple[list[CharacterIdentity], list[str]]:
        response = await request_with_retries(
            self.http,
            "POST",
            f"{self.base_url}/universe/ids/",
            params={"datasource": "tranquility", "language": "en"},
            json=names,
        )
        payload = response.json()
        returned = {
            item["name"].casefold(): (int(item["id"]), item["name"])
            for item in payload.get("characters", [])
        }

        matched: list[tuple[int, str]] = []
        invalid: list[str] = []
        for requested in names:
            result = returned.get(requested.casefold())
            if result is None:
                invalid.append(requested)
            else:
                matched.append(result)

        details = await asyncio.gather(
            *(self._character_detail(character_id) for character_id, _ in matched),
            return_exceptions=True,
        )
        valid_details: list[tuple[int, str, dict[str, object]]] = []
        for (character_id, name), detail in zip(matched, details, strict=True):
            if isinstance(detail, Exception):
                invalid.append(name)
                continue
            valid_details.append((character_id, name, detail))

        entity_ids: set[int] = set()
        for _, _, detail in valid_details:
            entity_ids.add(int(detail["corporation_id"]))
            if detail.get("alliance_id"):
                entity_ids.add(int(detail["alliance_id"]))
        entity_names = await self.resolve_entity_names(entity_ids)
        corporation_ids = {int(detail["corporation_id"]) for _, _, detail in valid_details}
        alliance_ids = {
            int(detail["alliance_id"])
            for _, _, detail in valid_details
            if detail.get("alliance_id")
        }
        corporation_results, alliance_results = await asyncio.gather(
            asyncio.gather(
                *(self._corporation_detail(entity_id) for entity_id in corporation_ids),
                return_exceptions=True,
            ),
            asyncio.gather(
                *(self._alliance_detail(entity_id) for entity_id in alliance_ids),
                return_exceptions=True,
            ),
        )
        corporations = {
            entity_id: result
            for entity_id, result in zip(corporation_ids, corporation_results, strict=True)
            if isinstance(result, dict)
        }
        alliances = {
            entity_id: result
            for entity_id, result in zip(alliance_ids, alliance_results, strict=True)
            if isinstance(result, dict)
        }

        identities: list[CharacterIdentity] = []
        for character_id, name, detail in valid_details:
            corporation_id = int(detail["corporation_id"])
            alliance_id = int(detail["alliance_id"]) if detail.get("alliance_id") else None
            corporation = corporations.get(corporation_id, {})
            alliance = alliances.get(alliance_id, {}) if alliance_id else {}
            identities.append(
                CharacterIdentity(
                    character_id=character_id,
                    name=name,
                    corporation_id=corporation_id,
                    corporation_name=str(
                        corporation.get("name")
                        or entity_names.get(corporation_id)
                        or f"军团 {corporation_id}"
                    ),
                    corporation_ticker=str(corporation.get("ticker") or ""),
                    alliance_id=alliance_id,
                    alliance_name=(
                        str(
                            alliance.get("name")
                            or entity_names.get(alliance_id)
                            or f"联盟 {alliance_id}"
                        )
                        if alliance_id
                        else None
                    ),
                    alliance_ticker=(
                        str(alliance.get("ticker") or "") if alliance_id else None
                    ),
                    birthday=_optional_datetime(detail.get("birthday")),
                    security_status=(
                        float(detail["security_status"])
                        if detail.get("security_status") is not None
                        else None
                    ),
                )
            )
        return identities, invalid

    async def resolve_entity_names(self, ids: Iterable[int]) -> dict[int, str]:
        unique = list(dict.fromkeys(int(entity_id) for entity_id in ids if entity_id))
        if not unique:
            return {}

        result: dict[int, str] = {}
        for start in range(0, len(unique), 1000):
            chunk = unique[start : start + 1000]
            response = await request_with_retries(
                self.http,
                "POST",
                f"{self.base_url}/universe/names/",
                params={"datasource": "tranquility"},
                json=chunk,
            )
            for item in response.json():
                result[int(item["id"])] = item["name"]
        return result

    async def fetch_ship_types(self, type_ids: Iterable[int]) -> dict[int, ShipTypeInfo]:
        unique = list(dict.fromkeys(int(type_id) for type_id in type_ids if type_id))
        missing = [type_id for type_id in unique if type_id not in self._type_cache]
        results = await asyncio.gather(
            *(self._ship_type(type_id) for type_id in missing), return_exceptions=True
        )
        for type_id, result in zip(missing, results, strict=True):
            if isinstance(result, ShipTypeInfo):
                self._type_cache[type_id] = result
        return {
            type_id: self._type_cache[type_id] for type_id in unique if type_id in self._type_cache
        }

    async def _character_detail(self, character_id: int) -> dict[str, object]:
        async with self.semaphore:
            response = await request_with_retries(
                self.http,
                "GET",
                f"{self.base_url}/characters/{character_id}/",
                params={"datasource": "tranquility"},
            )
            return response.json()

    async def _corporation_detail(self, corporation_id: int) -> dict[str, object]:
        cached = self._entity_cache_get("corporation", corporation_id)
        if cached is not None:
            return cached
        async with self.semaphore:
            response = await request_with_retries(
                self.http,
                "GET",
                f"{self.base_url}/corporations/{corporation_id}/",
                params={"datasource": "tranquility"},
            )
            payload = response.json()
        self._entity_cache_set("corporation", corporation_id, payload)
        return payload

    async def _alliance_detail(self, alliance_id: int) -> dict[str, object]:
        cached = self._entity_cache_get("alliance", alliance_id)
        if cached is not None:
            return cached
        async with self.semaphore:
            response = await request_with_retries(
                self.http,
                "GET",
                f"{self.base_url}/alliances/{alliance_id}/",
                params={"datasource": "tranquility"},
            )
            payload = response.json()
        self._entity_cache_set("alliance", alliance_id, payload)
        return payload

    async def _ship_type(self, type_id: int) -> ShipTypeInfo:
        localized = self.sde.type_info(type_id) if self.sde else None
        if localized is not None:
            name_zh, _name_en, group_id, group_name_zh, group_name_en, category_id = localized
            return ShipTypeInfo(
                type_id=type_id,
                name=name_zh,
                name_en=_name_en,
                group_id=group_id,
                group_name=group_name_zh,
                group_name_en=group_name_en,
                category_id=category_id,
                role=self.classifier.classify(type_id, group_name_en, category_id),
            )

        async with self.semaphore:
            response = await request_with_retries(
                self.http,
                "GET",
                f"{self.base_url}/universe/types/{type_id}/",
                params={"datasource": "tranquility", "language": "zh"},
            )
            payload = response.json()

        group_id = int(payload["group_id"])
        group_name, category_id = await self._group(group_id)
        return ShipTypeInfo(
            type_id=type_id,
            name=payload["name"],
            group_id=group_id,
            group_name=group_name,
            group_name_en=group_name,
            category_id=category_id,
            role=self.classifier.classify(type_id, group_name, category_id),
        )

    async def _group(self, group_id: int) -> tuple[str, int | None]:
        cached = self._entity_cache_get("group", group_id)
        if cached is not None:
            return cached
        async with self.semaphore:
            response = await request_with_retries(
                self.http,
                "GET",
                f"{self.base_url}/universe/groups/{group_id}/",
                params={"datasource": "tranquility", "language": "en"},
            )
            payload = response.json()
        result = (
            payload["name"],
            int(payload["category_id"]) if payload.get("category_id") else None,
        )
        self._entity_cache_set("group", group_id, result)
        return result

    def _entity_cache_get(self, namespace: str, entity_id: int) -> object | None:
        key = (namespace, int(entity_id))
        cached = self._entity_cache.get(key)
        if cached is None:
            return None
        expires_at, value = cached
        if expires_at <= time.monotonic():
            self._entity_cache.pop(key, None)
            return None
        self._entity_cache.move_to_end(key)
        return value

    def _entity_cache_set(self, namespace: str, entity_id: int, value: object) -> None:
        key = (namespace, int(entity_id))
        self._entity_cache[key] = (
            time.monotonic() + self.entity_cache_ttl_seconds,
            value,
        )
        self._entity_cache.move_to_end(key)
        while len(self._entity_cache) > self.entity_cache_entries:
            self._entity_cache.popitem(last=False)


def _optional_datetime(value: object) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


__all__ = ["ESIClient", "ExternalServiceError"]
