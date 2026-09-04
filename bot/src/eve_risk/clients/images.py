from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Iterable

import httpx


class EveImageClient:
    """Fetch and cache character portraits and ship icons from EVE Image Server."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str = "https://images.evetech.net",
        *,
        concurrency: int = 8,
        cache_entries: int = 256,
    ) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.semaphore = asyncio.Semaphore(concurrency)
        self.cache_entries = max(16, cache_entries)
        self._cache: OrderedDict[str, bytes] = OrderedDict()

    async def fetch_report_assets(
        self,
        character_ids: Iterable[int],
        ship_type_ids: Iterable[int],
        corporation_ids: Iterable[int] = (),
        alliance_ids: Iterable[int] = (),
    ) -> tuple[dict[int, bytes], dict[int, bytes], dict[int, bytes], dict[int, bytes]]:
        portraits, ships, corporations, alliances = await asyncio.gather(
            self.fetch_character_portraits(character_ids),
            self.fetch_ship_icons(ship_type_ids),
            self.fetch_corporation_logos(corporation_ids),
            self.fetch_alliance_logos(alliance_ids),
        )
        return portraits, ships, corporations, alliances

    async def fetch_character_portraits(
        self, character_ids: Iterable[int]
    ) -> dict[int, bytes]:
        return await self._fetch_many("characters", character_ids, "portrait", 128)

    async def fetch_ship_icons(self, type_ids: Iterable[int]) -> dict[int, bytes]:
        return await self._fetch_many("types", type_ids, "icon", 64)

    async def fetch_corporation_logos(
        self, corporation_ids: Iterable[int]
    ) -> dict[int, bytes]:
        return await self._fetch_many("corporations", corporation_ids, "logo", 64)

    async def fetch_alliance_logos(
        self, alliance_ids: Iterable[int]
    ) -> dict[int, bytes]:
        return await self._fetch_many("alliances", alliance_ids, "logo", 64)

    async def _fetch_many(
        self,
        category: str,
        entity_ids: Iterable[int],
        variant: str,
        size: int,
    ) -> dict[int, bytes]:
        unique = list(dict.fromkeys(int(entity_id) for entity_id in entity_ids if entity_id))
        results = await asyncio.gather(
            *(self._fetch(category, entity_id, variant, size) for entity_id in unique),
            return_exceptions=True,
        )
        return {
            entity_id: result
            for entity_id, result in zip(unique, results, strict=True)
            if isinstance(result, bytes)
        }

    async def _fetch(
        self,
        category: str,
        entity_id: int,
        variant: str,
        size: int,
    ) -> bytes:
        key = f"{category}:{entity_id}:{variant}:{size}"
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached

        async with self.semaphore:
            response = await self.http.get(
                f"{self.base_url}/{category}/{entity_id}/{variant}",
                params={"size": size},
                headers={"Accept": "image/png,image/jpeg,image/webp"},
            )
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if not content_type.startswith("image/"):
                raise ValueError(f"Unexpected EVE image content type: {content_type}")
            data = response.content
            if not data or len(data) > 2_000_000:
                raise ValueError("Invalid EVE image payload size")

        self._cache[key] = data
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_entries:
            self._cache.popitem(last=False)
        return data
