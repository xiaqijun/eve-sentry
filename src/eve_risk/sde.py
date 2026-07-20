from __future__ import annotations

import io
import json
import logging
import os
import re
import sqlite3
import tempfile
import zipfile
from collections.abc import Iterable
from pathlib import Path

import httpx

from eve_risk.domain import SolarSystemInfo

logger = logging.getLogger(__name__)

SDE_SCHEMA_VERSION = "2"
SDE_MEMBERS = (
    "groups.jsonl",
    "types.jsonl",
    "mapRegions.jsonl",
    "mapSolarSystems.jsonl",
)


class SDELocalization:
    """Read-only access to the compact Chinese index generated from CCP's official SDE."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._connection: sqlite3.Connection | None = None
        if self.path.is_file():
            try:
                connection = sqlite3.connect(
                    f"file:{self.path.as_posix()}?mode=ro",
                    uri=True,
                    check_same_thread=False,
                )
                schema_version = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()
                if schema_version and schema_version[0] == SDE_SCHEMA_VERSION:
                    self._connection = connection
                else:
                    connection.close()
                    logger.warning("SDE index schema is missing or unsupported: %s", self.path)
            except sqlite3.Error:
                logger.exception("Unable to open SDE index: %s", self.path)

    @property
    def available(self) -> bool:
        return self._connection is not None

    @property
    def build_number(self) -> str | None:
        if self._connection is None:
            return None
        row = self._connection.execute(
            "SELECT value FROM metadata WHERE key = 'build_number'"
        ).fetchone()
        return str(row[0]) if row else None

    def type_info(self, type_id: int) -> tuple[str, str, int, str, str, int | None] | None:
        if self._connection is None:
            return None
        row = self._connection.execute(
            """
            SELECT t.name_zh, t.name_en, t.group_id,
                   g.name_zh, g.name_en, g.category_id
            FROM types AS t
            JOIN groups AS g ON g.group_id = t.group_id
            WHERE t.type_id = ?
            """,
            (type_id,),
        ).fetchone()
        if row is None:
            return None
        return (
            str(row[0]),
            str(row[1]),
            int(row[2]),
            str(row[3]),
            str(row[4]),
            int(row[5]) if row[5] is not None else None,
        )

    def solar_system_name(self, solar_system_id: int) -> str | None:
        info = self.solar_system_info(solar_system_id)
        return info.name if info else None

    def solar_system_info(self, solar_system_id: int) -> SolarSystemInfo | None:
        if self._connection is None:
            return None
        row = self._connection.execute(
            """
            SELECT s.name_zh, s.region_id, r.name_zh
            FROM solar_systems AS s
            JOIN regions AS r ON r.region_id = s.region_id
            WHERE s.solar_system_id = ?
            """,
            (solar_system_id,),
        ).fetchone()
        if row is None:
            return None
        return SolarSystemInfo(
            solar_system_id=solar_system_id,
            name=str(row[0]),
            region_id=int(row[1]),
            region_name=str(row[2]),
        )

    def solar_systems(self, solar_system_ids: Iterable[int]) -> dict[int, SolarSystemInfo]:
        return {
            solar_system_id: info
            for solar_system_id in dict.fromkeys(int(item) for item in solar_system_ids)
            if (info := self.solar_system_info(solar_system_id)) is not None
        }

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None


def sync_sde(url: str, index_path: str | Path) -> bool:
    """Update the local index when CCP publishes a newer Tranquility SDE.

    Returns True when a new index was installed and False when the existing
    index is current or a remote check failed while a usable index exists.
    """

    destination = Path(index_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    current_build = _read_metadata(destination, "build_number")
    current_schema = _read_metadata(destination, "schema_version")

    try:
        with httpx.Client(
            follow_redirects=False,
            timeout=httpx.Timeout(60.0, read=300.0),
            headers={"User-Agent": "EveRiskAnalysis-SDE/0.1"},
        ) as client:
            response = client.head(url)
            if response.status_code >= 400:
                response.raise_for_status()
            download_url = response.headers.get("location", url)
            build_number = response.headers.get("x-sde-build-number") or _build_from_url(
                download_url
            )
            if (
                current_schema == SDE_SCHEMA_VERSION
                and current_build
                and build_number
                and current_build == build_number
            ):
                logger.info("Official SDE index is current (build %s)", current_build)
                return False

            with tempfile.NamedTemporaryFile(
                prefix="eve-sde-", suffix=".zip", dir=destination.parent, delete=False
            ) as archive_file:
                archive_path = Path(archive_file.name)
                with client.stream("GET", download_url) as download:
                    download.raise_for_status()
                    for chunk in download.iter_bytes(1024 * 1024):
                        archive_file.write(chunk)
    except Exception:
        if destination.is_file():
            logger.exception("Official SDE update failed; retaining the existing index")
            return False
        raise

    try:
        build_sde_index(archive_path, destination, build_number or "unknown")
    finally:
        archive_path.unlink(missing_ok=True)
    logger.info("Installed official SDE Chinese index build %s", build_number or "unknown")
    return True


def build_sde_index(archive_path: str | Path, index_path: str | Path, build_number: str) -> None:
    destination = Path(index_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)

    try:
        connection = sqlite3.connect(temporary)
        try:
            connection.executescript(
                """
                PRAGMA journal_mode = OFF;
                PRAGMA synchronous = OFF;
                CREATE TABLE metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE groups (
                    group_id INTEGER PRIMARY KEY,
                    name_zh TEXT NOT NULL,
                    name_en TEXT NOT NULL,
                    category_id INTEGER
                );
                CREATE TABLE types (
                    type_id INTEGER PRIMARY KEY,
                    name_zh TEXT NOT NULL,
                    name_en TEXT NOT NULL,
                    group_id INTEGER NOT NULL
                );
                CREATE TABLE regions (
                    region_id INTEGER PRIMARY KEY,
                    name_zh TEXT NOT NULL,
                    name_en TEXT NOT NULL
                );
                CREATE TABLE solar_systems (
                    solar_system_id INTEGER PRIMARY KEY,
                    name_zh TEXT NOT NULL,
                    name_en TEXT NOT NULL,
                    region_id INTEGER NOT NULL
                );
                """
            )
            connection.executemany(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                (("schema_version", SDE_SCHEMA_VERSION), ("build_number", build_number)),
            )

            with zipfile.ZipFile(archive_path) as archive:
                missing = set(SDE_MEMBERS) - set(archive.namelist())
                if missing:
                    raise ValueError(f"SDE archive is missing: {', '.join(sorted(missing))}")
                _insert_jsonl(
                    connection,
                    "INSERT INTO groups VALUES (?, ?, ?, ?)",
                    _group_rows(archive, "groups.jsonl"),
                )
                _insert_jsonl(
                    connection,
                    "INSERT INTO types VALUES (?, ?, ?, ?)",
                    _type_rows(archive, "types.jsonl"),
                )
                _insert_jsonl(
                    connection,
                    "INSERT INTO regions VALUES (?, ?, ?)",
                    _region_rows(archive, "mapRegions.jsonl"),
                )
                _insert_jsonl(
                    connection,
                    "INSERT INTO solar_systems VALUES (?, ?, ?, ?)",
                    _solar_system_rows(archive, "mapSolarSystems.jsonl"),
                )
            connection.commit()
            connection.execute("PRAGMA optimize")
        finally:
            connection.close()
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _insert_jsonl(
    connection: sqlite3.Connection, statement: str, rows: Iterable[tuple[object, ...]]
) -> None:
    batch: list[tuple[object, ...]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= 5000:
            connection.executemany(statement, batch)
            batch.clear()
    if batch:
        connection.executemany(statement, batch)


def _group_rows(
    archive: zipfile.ZipFile, member: str
) -> Iterable[tuple[int, str, str, int | None]]:
    for item in _read_jsonl(archive, member):
        names = item.get("name") or {}
        english = str(names.get("en") or f"Group {item['_key']}")
        yield (
            int(item["_key"]),
            str(names.get("zh") or english),
            english,
            int(item["categoryID"]) if item.get("categoryID") is not None else None,
        )


def _type_rows(archive: zipfile.ZipFile, member: str) -> Iterable[tuple[int, str, str, int]]:
    for item in _read_jsonl(archive, member):
        names = item.get("name") or {}
        english = str(names.get("en") or f"Type {item['_key']}")
        yield (
            int(item["_key"]),
            str(names.get("zh") or english),
            english,
            int(item["groupID"]),
        )


def _region_rows(archive: zipfile.ZipFile, member: str) -> Iterable[tuple[int, str, str]]:
    for item in _read_jsonl(archive, member):
        names = item.get("name") or {}
        english = str(names.get("en") or f"Region {item['_key']}")
        yield (int(item["_key"]), str(names.get("zh") or english), english)


def _solar_system_rows(
    archive: zipfile.ZipFile, member: str
) -> Iterable[tuple[int, str, str, int]]:
    for item in _read_jsonl(archive, member):
        names = item.get("name") or {}
        english = str(names.get("en") or f"Solar System {item['_key']}")
        yield (
            int(item["_key"]),
            str(names.get("zh") or english),
            english,
            int(item["regionID"]),
        )


def _read_jsonl(archive: zipfile.ZipFile, member: str) -> Iterable[dict[str, object]]:
    with archive.open(member) as binary:
        with io.TextIOWrapper(binary, encoding="utf-8") as text:
            for line in text:
                if line.strip():
                    yield json.loads(line)


def _read_metadata(path: Path, key: str) -> str | None:
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
            return str(row[0]) if row else None
        finally:
            connection.close()
    except sqlite3.Error:
        return None


def _build_from_url(url: str) -> str | None:
    match = re.search(r"static-data-(\d+)-(?:jsonl|yaml)\.zip", url)
    return match.group(1) if match else None


def main() -> None:
    from eve_risk.config import get_settings

    settings = get_settings()
    sync_sde(settings.sde_url, settings.sde_index_path)


if __name__ == "__main__":
    main()
