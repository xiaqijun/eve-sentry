"""Import star-map topology from an EVE Online SDE export."""

from __future__ import annotations

from pathlib import Path
from typing import Any


class SdeImportError(RuntimeError):
    """Raised when the configured SDE export cannot be imported."""


class SdeMapImporter:
    """Read solar-system topology from a local SDE export."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def load_map(
        self,
        region_ids: list[int] | None = None,
        system_ids: list[int] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """Return map nodes and links from the configured SDE directory."""
        yaml = self._load_yaml_module()
        if not self.root.exists():
            raise SdeImportError(f"SDE path does not exist: {self.root}")

        systems_path = self._resolve_table_path("mapSolarSystems.yaml")
        jumps_path = self._resolve_table_path("mapSolarSystemJumps.yaml")
        stargates_path = self._resolve_table_path("mapStargates.yaml")
        link_filename = None
        if jumps_path is not None:
            link_filename = "mapSolarSystemJumps.yaml"
        elif stargates_path is not None:
            link_filename = "mapStargates.yaml"

        if systems_path is None or link_filename is None:
            return self._load_universe_map(
                yaml,
                region_ids=region_ids or [],
                system_ids=system_ids or [],
            )

        systems_rows = self._load_table(yaml, "mapSolarSystems.yaml")
        jumps_rows = self._load_table(yaml, link_filename)
        constellation_rows = self._load_table(
            yaml,
            "mapConstellations.yaml",
            required=False,
        )
        region_rows = self._load_table(yaml, "mapRegions.yaml", required=False)
        name_rows = self._load_table(yaml, "invNames.yaml", required=False)
        self._materialize_table_id(systems_rows, "solarSystemID")
        self._materialize_table_id(constellation_rows, "constellationID")
        self._materialize_table_id(region_rows, "regionID")

        name_by_id = self._name_by_id(name_rows)
        constellation_to_region = self._constellation_to_region(constellation_rows)
        region_name_by_id = self._region_name_by_id(region_rows, name_by_id)

        selected_systems = self._select_system_rows(
            systems_rows,
            region_ids=region_ids or [],
            system_ids=system_ids or [],
            constellation_to_region=constellation_to_region,
        )
        if not selected_systems:
            raise SdeImportError("SDE import did not resolve any solar systems")

        selected_ids = {
            self._require_int(row, ("solarSystemID", "solar_system_id", "system_id"))
            for row in selected_systems
        }

        systems_payload = self._build_system_payload(
            selected_systems,
            name_by_id=name_by_id,
            constellation_to_region=constellation_to_region,
            region_name_by_id=region_name_by_id,
        )
        links_payload = self._build_link_payload(
            jumps_rows,
            selected_ids=selected_ids,
            names_by_id={
                int(item["system_id"]): str(item["name"])
                for item in systems_payload
                if item.get("system_id") is not None
            },
        )
        return systems_payload, links_payload

    def _load_universe_map(
        self,
        yaml: Any,
        region_ids: list[int],
        system_ids: list[int],
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        universe_root = self._resolve_universe_root()
        if universe_root is None:
            raise SdeImportError(
                "SDE export is missing both BSD map tables and universe layout"
            )

        region_filter = {int(item) for item in region_ids if int(item) > 0}
        system_filter = {int(item) for item in system_ids if int(item) > 0}
        selected_rows: list[dict[str, Any]] = []
        region_name_by_id: dict[int, str] = {}
        gate_to_system: dict[int, int] = {}
        gate_destinations: dict[int, int] = {}

        for region_path in sorted(universe_root.iterdir(), key=lambda item: item.name.casefold()):
            if not region_path.is_dir():
                continue
            region_meta = self._load_yaml_file(
                yaml,
                region_path / "region.yaml",
                required=False,
            )
            region_id = self._optional_int(region_meta.get("regionID"))
            region_name = self._display_name(region_path.name)
            if region_id is not None:
                region_name_by_id[region_id] = region_name

            if region_filter and region_id not in region_filter and not system_filter:
                continue

            for constellation_path in sorted(
                region_path.iterdir(),
                key=lambda item: item.name.casefold(),
            ):
                if not constellation_path.is_dir():
                    continue
                constellation_file = constellation_path / "constellation.yaml"
                if not constellation_file.exists():
                    continue
                constellation_meta = self._load_yaml_file(
                    yaml,
                    constellation_file,
                    required=False,
                )
                constellation_id = self._optional_int(
                    constellation_meta.get("constellationID")
                )

                for system_path in sorted(
                    constellation_path.iterdir(),
                    key=lambda item: item.name.casefold(),
                ):
                    if not system_path.is_dir():
                        continue
                    system_file = system_path / "solarsystem.yaml"
                    if not system_file.exists():
                        continue
                    system_meta = self._load_yaml_file(yaml, system_file, required=True)
                    system_id = self._optional_int(system_meta.get("solarSystemID"))
                    if system_id is None:
                        continue
                    if not self._system_selected(
                        system_id,
                        region_id,
                        region_filter=region_filter,
                        system_filter=system_filter,
                    ):
                        continue

                    center = system_meta.get("center")
                    selected_rows.append(
                        {
                            "solarSystemID": system_id,
                            "solarSystemName": system_path.name,
                            "constellationID": constellation_id,
                            "regionID": region_id,
                            "regionName": region_name,
                            "security": system_meta.get("security"),
                            "x": self._vector_value(center, 0),
                            "z": self._vector_value(center, 2),
                        }
                    )
                    self._collect_universe_stargates(
                        system_meta.get("stargates"),
                        system_id=system_id,
                        gate_to_system=gate_to_system,
                        gate_destinations=gate_destinations,
                    )

        if not selected_rows:
            raise SdeImportError("SDE import did not resolve any solar systems")

        systems_payload = self._build_system_payload(
            selected_rows,
            name_by_id={},
            constellation_to_region={},
            region_name_by_id=region_name_by_id,
        )
        links_payload = self._build_universe_link_payload(
            gate_to_system=gate_to_system,
            gate_destinations=gate_destinations,
            names_by_id={
                int(item["system_id"]): str(item["name"])
                for item in systems_payload
                if item.get("system_id") is not None
            },
        )
        return systems_payload, links_payload

    def _load_yaml_module(self):
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError as exc:
            raise SdeImportError(
                "PyYAML is required for SDE import. Install it with pip install PyYAML."
            ) from exc
        return yaml

    def _load_table(
        self,
        yaml: Any,
        filename: str,
        required: bool = True,
    ) -> list[dict[str, Any]]:
        path = self._resolve_table_path(filename)
        if path is None:
            if required:
                raise SdeImportError(f"missing SDE table: {filename}")
            return []

        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise SdeImportError(f"failed to read {path}: {exc}") from exc
        except Exception as exc:
            raise SdeImportError(f"failed to parse {path}: {exc}") from exc
        return self._normalize_rows(payload)

    def _resolve_table_path(self, filename: str) -> Path | None:
        candidates = (
            self.root / "bsd" / filename,
            self.root / filename,
            self.root / "sde" / "bsd" / filename,
        )
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def _resolve_universe_root(self) -> Path | None:
        candidates = (
            self.root / "universe" / "eve",
            self.root / "sde" / "universe" / "eve",
        )
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def _load_yaml_file(
        self,
        yaml: Any,
        path: Path,
        required: bool,
    ) -> dict[str, Any]:
        if not path.exists():
            if required:
                raise SdeImportError(f"missing SDE file: {path}")
            return {}
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise SdeImportError(f"failed to read {path}: {exc}") from exc
        except Exception as exc:
            raise SdeImportError(f"failed to parse {path}: {exc}") from exc
        return dict(payload) if isinstance(payload, dict) else {}

    def _normalize_rows(self, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [dict(item) for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            rows: list[dict[str, Any]] = []
            for key, value in payload.items():
                if not isinstance(value, dict):
                    continue
                row = dict(value)
                if "itemID" not in row:
                    numeric_key = self._optional_int(key)
                    if numeric_key is not None:
                        row.setdefault("itemID", numeric_key)
                rows.append(row)
            return rows
        return []

    def _name_by_id(self, rows: list[dict[str, Any]]) -> dict[int, str]:
        mapping: dict[int, str] = {}
        for row in rows:
            item_id = self._optional_int(
                row.get("itemID", row.get("item_id", row.get("id")))
            )
            name = self._name_value(
                row.get("itemName", row.get("item_name", row.get("name")))
            )
            if item_id is None or not name:
                continue
            mapping[item_id] = name
        return mapping

    def _constellation_to_region(
        self,
        rows: list[dict[str, Any]],
    ) -> dict[int, int]:
        mapping: dict[int, int] = {}
        for row in rows:
            constellation_id = self._optional_int(
                row.get(
                    "constellationID",
                    row.get("constellation_id", row.get("itemID")),
                )
            )
            region_id = self._optional_int(row.get("regionID", row.get("region_id")))
            if constellation_id is None or region_id is None:
                continue
            mapping[constellation_id] = region_id
        return mapping

    def _region_name_by_id(
        self,
        rows: list[dict[str, Any]],
        fallback_names: dict[int, str],
    ) -> dict[int, str]:
        mapping: dict[int, str] = {}
        for row in rows:
            region_id = self._optional_int(
                row.get("regionID", row.get("region_id", row.get("itemID")))
            )
            name = self._name_value(
                row.get("regionName", row.get("region_name", row.get("name")))
            )
            if region_id is None:
                continue
            mapping[region_id] = name or fallback_names.get(region_id, "")
        return mapping

    def _select_system_rows(
        self,
        rows: list[dict[str, Any]],
        region_ids: list[int],
        system_ids: list[int],
        constellation_to_region: dict[int, int],
    ) -> list[dict[str, Any]]:
        region_filter = {int(item) for item in region_ids if int(item) > 0}
        system_filter = {int(item) for item in system_ids if int(item) > 0}
        if not region_filter and not system_filter:
            return list(rows)

        selected: list[dict[str, Any]] = []
        for row in rows:
            system_id = self._optional_int(
                row.get("solarSystemID", row.get("solar_system_id", row.get("system_id")))
            )
            if system_id is None:
                continue
            if system_id in system_filter:
                selected.append(row)
                continue

            constellation_id = self._optional_int(
                row.get("constellationID", row.get("constellation_id"))
            )
            region_id = self._optional_int(row.get("regionID", row.get("region_id")))
            if region_id is None and constellation_id is not None:
                region_id = constellation_to_region.get(constellation_id)
            if region_id is not None and region_id in region_filter:
                selected.append(row)
        return selected

    def _build_system_payload(
        self,
        rows: list[dict[str, Any]],
        name_by_id: dict[int, str],
        constellation_to_region: dict[int, int],
        region_name_by_id: dict[int, str],
    ) -> list[dict[str, Any]]:
        projected = self._project_coordinates(rows)
        systems: list[dict[str, Any]] = []
        for row in sorted(rows, key=self._system_sort_key):
            system_id = self._require_int(
                row,
                ("solarSystemID", "solar_system_id", "system_id"),
            )
            name = self._system_name(row, name_by_id)
            if not name:
                raise SdeImportError(f"system {system_id} is missing a name")

            constellation_id = self._optional_int(
                row.get("constellationID", row.get("constellation_id"))
            )
            region_id = self._optional_int(row.get("regionID", row.get("region_id")))
            if region_id is None and constellation_id is not None:
                region_id = constellation_to_region.get(constellation_id)
            region_name = region_name_by_id.get(region_id or 0, "") or "Unknown region"
            x, y = projected[system_id]
            systems.append(
                {
                    "system_id": system_id,
                    "name": name,
                    "x": x,
                    "y": y,
                    "region": region_name,
                    "security": self._optional_float(
                        row.get("security", row.get("securityStatus"))
                    ),
                }
            )
        return systems

    def _build_link_payload(
        self,
        rows: list[dict[str, Any]],
        selected_ids: set[int],
        names_by_id: dict[int, str],
    ) -> list[dict[str, str]]:
        links: list[dict[str, str]] = []
        seen: set[tuple[int, int]] = set()
        for row in rows:
            source_id = self._optional_int(
                row.get(
                    "fromSolarSystemID",
                    row.get("from_solar_system_id", row.get("solarSystemID")),
                )
            )
            target_id = self._target_system_id(row)
            if source_id is None or target_id is None:
                continue
            if source_id == target_id:
                continue
            if source_id not in selected_ids or target_id not in selected_ids:
                continue
            edge = tuple(sorted((source_id, target_id)))
            if edge in seen:
                continue
            seen.add(edge)
            source_name = names_by_id.get(source_id, "")
            target_name = names_by_id.get(target_id, "")
            if not source_name or not target_name:
                continue
            links.append({"from": source_name, "to": target_name})
        return links

    def _build_universe_link_payload(
        self,
        gate_to_system: dict[int, int],
        gate_destinations: dict[int, int],
        names_by_id: dict[int, str],
    ) -> list[dict[str, str]]:
        links: list[dict[str, str]] = []
        seen: set[tuple[int, int]] = set()
        for gate_id, destination_gate_id in gate_destinations.items():
            source_id = gate_to_system.get(gate_id)
            target_id = gate_to_system.get(destination_gate_id)
            if source_id is None or target_id is None or source_id == target_id:
                continue
            edge = tuple(sorted((source_id, target_id)))
            if edge in seen:
                continue
            seen.add(edge)
            source_name = names_by_id.get(source_id, "")
            target_name = names_by_id.get(target_id, "")
            if not source_name or not target_name:
                continue
            links.append({"from": source_name, "to": target_name})
        return links

    def _project_coordinates(
        self,
        rows: list[dict[str, Any]],
    ) -> dict[int, tuple[float, float]]:
        points: list[tuple[int, float, float]] = []
        for row in rows:
            system_id = self._optional_int(
                row.get("solarSystemID", row.get("solar_system_id", row.get("system_id")))
            )
            raw_x, raw_y = self._coordinate_pair(row)
            if system_id is None or raw_x is None or raw_y is None:
                continue
            points.append((system_id, raw_x, raw_y))

        if not points:
            raise SdeImportError("SDE systems are missing x/z coordinates")

        xs = [item[1] for item in points]
        ys = [item[2] for item in points]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        span_x = max(max_x - min_x, 1.0)
        span_y = max(max_y - min_y, 1.0)

        projected: dict[int, tuple[float, float]] = {}
        for system_id, raw_x, raw_y in points:
            x = 80.0 + ((raw_x - min_x) / span_x) * 1200.0
            y = 80.0 + ((max_y - raw_y) / span_y) * 820.0
            projected[system_id] = (round(x, 1), round(y, 1))
        return projected

    def _system_sort_key(self, row: dict[str, Any]) -> tuple[str, int]:
        name = self._name_value(
            row.get("solarSystemName", row.get("solar_system_name", row.get("name")))
        )
        system_id = self._optional_int(
            row.get("solarSystemID", row.get("solar_system_id", row.get("system_id")))
        )
        return (name.casefold(), system_id or 0)

    def _system_name(
        self,
        row: dict[str, Any],
        fallback_names: dict[int, str],
    ) -> str:
        name = self._name_value(
            row.get("solarSystemName", row.get("solar_system_name", row.get("name")))
        )
        if name:
            return name
        system_id = self._optional_int(
            row.get("solarSystemID", row.get("solar_system_id", row.get("system_id")))
        )
        if system_id is None:
            return ""
        return fallback_names.get(system_id, "")

    def _system_selected(
        self,
        system_id: int,
        region_id: int | None,
        region_filter: set[int],
        system_filter: set[int],
    ) -> bool:
        if not region_filter and not system_filter:
            return True
        if system_id in system_filter:
            return True
        return region_id is not None and region_id in region_filter

    def _collect_universe_stargates(
        self,
        stargates: Any,
        system_id: int,
        gate_to_system: dict[int, int],
        gate_destinations: dict[int, int],
    ) -> None:
        if not isinstance(stargates, dict):
            return
        for gate_key, gate_meta in stargates.items():
            gate_id = self._optional_int(gate_key)
            if gate_id is None and isinstance(gate_meta, dict):
                gate_id = self._optional_int(gate_meta.get("id"))
            if gate_id is None:
                continue
            gate_to_system[gate_id] = system_id
            if not isinstance(gate_meta, dict):
                continue
            destination_id = self._optional_int(gate_meta.get("destination"))
            if destination_id is not None:
                gate_destinations[gate_id] = destination_id

    def _vector_value(self, value: Any, index: int) -> float | None:
        if not isinstance(value, (list, tuple)) or len(value) <= index:
            return None
        return self._optional_float(value[index])

    def _display_name(self, value: str) -> str:
        return value.strip() or "Unknown region"

    def _materialize_table_id(
        self,
        rows: list[dict[str, Any]],
        field_name: str,
    ) -> None:
        for row in rows:
            if field_name in row:
                continue
            item_id = self._optional_int(row.get("itemID"))
            if item_id is not None:
                row[field_name] = item_id

    def _target_system_id(self, row: dict[str, Any]) -> int | None:
        direct_id = self._optional_int(
            row.get("toSolarSystemID", row.get("to_solar_system_id"))
        )
        if direct_id is not None:
            return direct_id
        destination = row.get("destination")
        if isinstance(destination, dict):
            return self._optional_int(
                destination.get(
                    "solarSystemID",
                    destination.get("destinationSolarSystemID"),
                )
            )
        return None

    def _coordinate_pair(self, row: dict[str, Any]) -> tuple[float | None, float | None]:
        position_2d = row.get("position2D")
        if isinstance(position_2d, dict):
            pos_x = self._optional_float(position_2d.get("x"))
            pos_y = self._optional_float(position_2d.get("y"))
            if pos_x is not None and pos_y is not None:
                return pos_x, pos_y

        raw_x = self._optional_float(row.get("x"))
        raw_y = self._optional_float(row.get("z", row.get("y")))
        if raw_x is not None and raw_y is not None:
            return raw_x, raw_y

        position = row.get("position")
        if isinstance(position, dict):
            pos_x = self._optional_float(position.get("x"))
            pos_z = self._optional_float(position.get("z"))
            if pos_x is not None and pos_z is not None:
                return pos_x, pos_z

        return None, None

    def _require_int(
        self,
        row: dict[str, Any],
        keys: tuple[str, ...],
    ) -> int:
        for key in keys:
            value = self._optional_int(row.get(key))
            if value is not None:
                return value
        raise SdeImportError(f"row is missing required integer field {keys[0]}")

    def _optional_int(self, value: Any) -> int | None:
        if value in {None, ""}:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _optional_float(self, value: Any) -> float | None:
        if value in {None, ""}:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _string_value(self, value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        return ""

    def _name_value(self, value: Any) -> str:
        if isinstance(value, dict):
            for key in ("en", "zh", "ru", "de", "fr", "es", "ja", "ko"):
                text = self._string_value(value.get(key))
                if text:
                    return text
            for item in value.values():
                text = self._string_value(item)
                if text:
                    return text
            return ""
        return self._string_value(value)
