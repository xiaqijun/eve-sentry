"""Configurable star-map data sources for the intel server."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from app.server.intel_store import DEFAULT_LINKS, DEFAULT_SYSTEMS, StarSystem, utc_now_iso


MAP_CONFIG_SCHEMA_VERSION = "intel_map_config.v1"
MAP_SOURCES = {"builtin", "manual", "esi", "sde"}
MAP_LAYOUT_MODES = {"clusters", "manual", "sde"}


class MapConfigStore:
    """Persist and build configurable star-map data for the intel server."""

    def __init__(self, path: str | Path = "intel_map.json") -> None:
        self.path = Path(path)
        self._config = self._load()

    def to_dict(self) -> dict[str, Any]:
        """Return the normalized persisted map configuration."""
        data = dict(self._config)
        data["schema_version"] = MAP_CONFIG_SCHEMA_VERSION
        data["systems"] = [dict(item) for item in self._config.get("systems", [])]
        data["links"] = [dict(item) for item in self._config.get("links", [])]
        return data

    def update(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Update the map configuration and persist it."""
        config = dict(self._config)
        if "source" in payload:
            source = str(payload.get("source") or "").strip().casefold()
            if source not in MAP_SOURCES:
                raise ValueError("map source must be one of builtin, manual, esi, or sde")
            config["source"] = source

        if "layout_mode" in payload:
            layout_mode = str(payload.get("layout_mode") or "").strip().casefold()
            if layout_mode not in MAP_LAYOUT_MODES:
                raise ValueError("layout_mode must be one of clusters, manual, or sde")
            config["layout_mode"] = layout_mode

        if "region_ids" in payload:
            config["region_ids"] = self._normalize_int_list(payload.get("region_ids"))
        if "system_ids" in payload:
            config["system_ids"] = self._normalize_int_list(payload.get("system_ids"))
        if "sde_path" in payload:
            config["sde_path"] = str(payload.get("sde_path") or "").strip()
        if "systems" in payload:
            config["systems"] = self._normalize_system_entries(payload.get("systems"))
        if "links" in payload:
            config["links"] = self._normalize_link_entries(payload.get("links"))

        self._config = self._finalize(config)
        self.save()
        return self.to_dict()

    def build_map(
        self,
        resolver: Any | None = None,
        refresh_if_needed: bool = False,
    ) -> tuple[dict[str, StarSystem], list[tuple[str, str]]]:
        """Build map systems and links from the configured source."""
        source = self.source
        if source == "builtin":
            return self._builtin_map()

        if source == "manual":
            systems = self._configured_systems()
            links = self._configured_links(systems)
            return self._fallback_if_empty(systems, links)

        if source == "sde":
            if refresh_if_needed or not self._config.get("systems"):
                try:
                    self.refresh_from_sde()
                except Exception as exc:
                    self._record_refresh_error(str(exc))
            systems = self._configured_systems()
            links = self._configured_links(systems)
            return self._fallback_if_empty(systems, links)

        if source == "esi":
            if (refresh_if_needed or not self._config.get("systems")) and resolver is not None:
                try:
                    self.refresh_from_esi(resolver)
                except Exception as exc:
                    self._record_refresh_error(str(exc))
            systems = self._configured_systems()
            links = self._configured_links(systems)
            return self._fallback_if_empty(systems, links)

        return self._builtin_map()

    def refresh_from_source(self, resolver: Any | None = None) -> dict[str, Any]:
        """Refresh map data according to the configured source."""
        source = self.source
        if source == "builtin":
            self._config = self._finalize(
                {
                    **self._config,
                    "systems": [],
                    "links": [],
                    "last_refreshed_at": utc_now_iso(),
                    "last_refresh_error": "",
                }
            )
            self.save()
            return self.to_dict()
        if source == "manual":
            self._config = self._finalize(
                {
                    **self._config,
                    "last_refreshed_at": utc_now_iso(),
                    "last_refresh_error": "",
                }
            )
            self.save()
            return self.to_dict()
        if source == "sde":
            return self.refresh_from_sde()
        if source == "esi":
            return self.refresh_from_esi(resolver)
        raise ValueError(f"unsupported map source: {source}")

    def refresh_from_sde(self, sde_path: str | Path | None = None) -> dict[str, Any]:
        """Refresh the configured map data from an official SDE export."""
        from app.server.sde_map import SdeMapImporter

        raw_path = str(sde_path or self._config.get("sde_path") or "").strip()
        if not raw_path:
            raise ValueError("sde_path is required for SDE map refresh")
        effective_path = Path(raw_path)

        importer = SdeMapImporter(effective_path)
        systems_payload, links_payload = importer.load_map(
            region_ids=self.region_ids,
            system_ids=self.system_ids,
        )
        self._config = self._finalize(
            {
                **self._config,
                "source": "sde",
                "layout_mode": "sde",
                "sde_path": str(effective_path),
                "systems": systems_payload,
                "links": links_payload,
                "last_refreshed_at": utc_now_iso(),
                "last_refresh_error": "",
            }
        )
        self.save()
        return self.to_dict()

    def refresh_from_esi(self, resolver: Any | None) -> dict[str, Any]:
        """Refresh the configured map data from public ESI."""
        if resolver is None:
            raise ValueError("ESI resolver is required to refresh map data")

        region_ids = self.region_ids
        explicit_system_ids = self.system_ids
        if not region_ids and not explicit_system_ids:
            raise ValueError("region_ids or system_ids are required for ESI map refresh")

        region_names: dict[int, str] = {}
        constellation_to_region: dict[int, int] = {}
        constellation_names: dict[int, str] = {}
        target_system_ids: set[int] = set(explicit_system_ids)

        for region_id in region_ids:
            region = resolver.region_profile(region_id)
            region_names[region_id] = str(region.get("name") or f"Region {region_id}")
            for constellation_id in self._normalize_int_list(
                region.get("constellations", [])
            ):
                constellation_to_region[constellation_id] = region_id
                constellation = resolver.constellation_profile(constellation_id)
                constellation_names[constellation_id] = str(
                    constellation.get("name") or f"Constellation {constellation_id}"
                )
                for system_id in self._normalize_int_list(
                    constellation.get("systems", [])
                ):
                    target_system_ids.add(system_id)

        if not target_system_ids:
            raise ValueError("ESI map refresh did not resolve any solar systems")

        system_profiles: dict[int, dict[str, Any]] = {}
        for system_id in sorted(target_system_ids):
            profile = resolver.system_profile(system_id)
            system_profiles[system_id] = dict(profile)
            constellation_id = self._optional_int(profile.get("constellation_id"))
            if constellation_id is None:
                continue
            if constellation_id not in constellation_names:
                constellation = resolver.constellation_profile(constellation_id)
                constellation_names[constellation_id] = str(
                    constellation.get("name") or f"Constellation {constellation_id}"
                )
                region_id = self._optional_int(constellation.get("region_id"))
                if region_id is not None:
                    constellation_to_region[constellation_id] = region_id
                    if region_id not in region_names:
                        region = resolver.region_profile(region_id)
                        region_names[region_id] = str(
                            region.get("name") or f"Region {region_id}"
                        )

        links_by_id: set[tuple[int, int]] = set()
        for system_id, profile in system_profiles.items():
            for stargate_id in self._normalize_int_list(profile.get("stargates", [])):
                gate = resolver.stargate_profile(stargate_id)
                destination_id = self._optional_int(gate.get("destination_system_id"))
                if destination_id is None or destination_id not in target_system_ids:
                    continue
                if destination_id == system_id:
                    continue
                links_by_id.add(tuple(sorted((system_id, destination_id))))

        systems_payload = self._generate_clustered_systems(
            system_profiles,
            constellation_to_region=constellation_to_region,
            region_names=region_names,
        )
        name_by_id = {
            self._optional_int(item.get("system_id")): str(item.get("name") or "").strip()
            for item in systems_payload
        }
        links_payload = []
        for source_id, target_id in sorted(links_by_id):
            source_name = name_by_id.get(source_id, "")
            target_name = name_by_id.get(target_id, "")
            if source_name and target_name:
                links_payload.append({"from": source_name, "to": target_name})

        self._config = self._finalize(
            {
                **self._config,
                "source": "esi",
                "layout_mode": "clusters",
                "systems": systems_payload,
                "links": links_payload,
                "last_refreshed_at": utc_now_iso(),
                "last_refresh_error": "",
            }
        )
        self.save()
        return self.to_dict()

    @property
    def source(self) -> str:
        return str(self._config.get("source") or "builtin")

    @property
    def region_ids(self) -> list[int]:
        return list(self._config.get("region_ids", []))

    @property
    def system_ids(self) -> list[int]:
        return list(self._config.get("system_ids", []))

    @property
    def sde_path(self) -> str:
        return str(self._config.get("sde_path") or "")

    def save(self) -> None:
        """Persist the normalized map configuration."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _builtin_map(self) -> tuple[dict[str, StarSystem], list[tuple[str, str]]]:
        return dict(DEFAULT_SYSTEMS), list(DEFAULT_LINKS)

    def _configured_systems(self) -> dict[str, StarSystem]:
        systems: dict[str, StarSystem] = {}
        for item in self._config.get("systems", []):
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            systems[name] = StarSystem(
                name=name,
                x=float(item.get("x", 0.0)),
                y=float(item.get("y", 0.0)),
                region=str(item.get("region") or "Unknown region"),
                security=self._optional_float(item.get("security")),
                system_id=self._optional_int(item.get("system_id")),
            )
        return systems

    def _configured_links(
        self,
        systems: dict[str, StarSystem],
    ) -> list[tuple[str, str]]:
        links: list[tuple[str, str]] = []
        for item in self._config.get("links", []):
            source = str(item.get("from") or "").strip()
            target = str(item.get("to") or "").strip()
            if not source or not target or source == target:
                continue
            if source not in systems or target not in systems:
                continue
            links.append((source, target))
        return links

    def _fallback_if_empty(
        self,
        systems: dict[str, StarSystem],
        links: list[tuple[str, str]],
    ) -> tuple[dict[str, StarSystem], list[tuple[str, str]]]:
        if systems:
            return systems, links
        return self._builtin_map()

    def _generate_clustered_systems(
        self,
        system_profiles: dict[int, dict[str, Any]],
        constellation_to_region: dict[int, int],
        region_names: dict[int, str],
    ) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for system_id, profile in system_profiles.items():
            name = str(profile.get("name") or f"System {system_id}").strip()
            constellation_id = self._optional_int(profile.get("constellation_id"))
            region_id = (
                constellation_to_region.get(constellation_id) if constellation_id else None
            )
            region_name = (
                region_names.get(region_id, "Unknown region") if region_id else "Unknown region"
            )
            grouped.setdefault(region_name, []).append(
                {
                    "system_id": system_id,
                    "name": name,
                    "security": self._optional_float(
                        profile.get("security_status", profile.get("security"))
                    ),
                    "region": region_name,
                    "constellation_id": constellation_id,
                }
            )

        region_names_sorted = sorted(grouped)
        region_columns = max(1, math.ceil(math.sqrt(len(region_names_sorted) or 1)))
        systems: list[dict[str, Any]] = []
        for region_index, region_name in enumerate(region_names_sorted):
            base_x = 140 + (region_index % region_columns) * 300
            base_y = 120 + (region_index // region_columns) * 220
            region_systems = sorted(
                grouped[region_name],
                key=lambda item: (
                    item.get("constellation_id") or 0,
                    str(item.get("name") or ""),
                ),
            )
            local_columns = max(1, math.ceil(math.sqrt(len(region_systems))))
            for item_index, item in enumerate(region_systems):
                x = base_x + (item_index % local_columns) * 56
                y = base_y + (item_index // local_columns) * 44
                systems.append(
                    {
                        "system_id": item["system_id"],
                        "name": item["name"],
                        "x": float(round(x, 1)),
                        "y": float(round(y, 1)),
                        "region": item["region"],
                        "security": item["security"],
                    }
                )
        return systems

    def _record_refresh_error(self, message: str) -> None:
        self._config = self._finalize(
            {
                **self._config,
                "last_refresh_error": str(message or "").strip(),
            }
        )
        self.save()

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._default_config()
        if not isinstance(raw, dict):
            return self._default_config()
        return self._finalize(raw)

    def _default_config(self) -> dict[str, Any]:
        return self._finalize(
            {
                "source": "builtin",
                "layout_mode": "clusters",
                "region_ids": [],
                "system_ids": [],
                "sde_path": "",
                "systems": [],
                "links": [],
                "last_refreshed_at": "",
                "last_refresh_error": "",
            }
        )

    def _finalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        source = str(payload.get("source") or "builtin").strip().casefold()
        if source not in MAP_SOURCES:
            source = "builtin"
        layout_mode = str(payload.get("layout_mode") or "clusters").strip().casefold()
        if layout_mode not in MAP_LAYOUT_MODES:
            layout_mode = "clusters"
        return {
            "source": source,
            "layout_mode": layout_mode,
            "region_ids": self._normalize_int_list(payload.get("region_ids")),
            "system_ids": self._normalize_int_list(payload.get("system_ids")),
            "sde_path": str(payload.get("sde_path") or "").strip(),
            "systems": self._normalize_system_entries(payload.get("systems")),
            "links": self._normalize_link_entries(payload.get("links")),
            "last_refreshed_at": str(payload.get("last_refreshed_at") or "").strip(),
            "last_refresh_error": str(payload.get("last_refresh_error") or "").strip(),
        }

    def _normalize_system_entries(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        systems: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name or name in seen:
                continue
            try:
                x = float(item.get("x", 0.0))
                y = float(item.get("y", 0.0))
            except (TypeError, ValueError):
                continue
            seen.add(name)
            systems.append(
                {
                    "system_id": self._optional_int(item.get("system_id")),
                    "name": name,
                    "x": float(round(x, 1)),
                    "y": float(round(y, 1)),
                    "region": str(item.get("region") or "Unknown region").strip()
                    or "Unknown region",
                    "security": self._optional_float(item.get("security")),
                }
            )
        return systems

    def _normalize_link_entries(self, value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        links: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in value:
            if isinstance(item, dict):
                source = str(item.get("from") or "").strip()
                target = str(item.get("to") or "").strip()
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                source = str(item[0] or "").strip()
                target = str(item[1] or "").strip()
            else:
                continue
            if not source or not target or source == target:
                continue
            pair = (source, target)
            if pair in seen:
                continue
            seen.add(pair)
            links.append({"from": source, "to": target})
        return links

    def _normalize_int_list(self, value: Any) -> list[int]:
        if value is None:
            return []
        if isinstance(value, int):
            value = [value]
        if not isinstance(value, list):
            return []
        seen: set[int] = set()
        result: list[int] = []
        for item in value:
            number = self._optional_int(item)
            if number is None or number in seen:
                continue
            seen.add(number)
            result.append(number)
        return result

    def _optional_int(self, value: Any) -> int | None:
        if value in {None, ""}:
            return None
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    def _optional_float(self, value: Any) -> float | None:
        if value in {None, ""}:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
