"""Download and extract official EVE Online SDE packages."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path, PurePosixPath
from urllib.request import urlopen
import zipfile


STATIC_DATA_ROOT = "https://developers.eveonline.com/static-data/tranquility/"
CORE_MAP_FILES = {
    "_sde.yaml",
    "invNames.yaml",
    "mapConstellations.yaml",
    "mapRegions.yaml",
    "mapSolarSystems.yaml",
    "mapSolarSystemJumps.yaml",
    "mapStargates.yaml",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download and extract the official EVE SDE package."
    )
    parser.add_argument(
        "--target",
        default=".runtime/sde",
        help="directory used for downloaded and extracted SDE files",
    )
    parser.add_argument(
        "--build",
        type=int,
        default=0,
        help="specific SDE build number; defaults to the latest official build",
    )
    parser.add_argument(
        "--region-name",
        action="append",
        default=[],
        help="extract only these universe region directories from the YAML package",
    )
    parser.add_argument(
        "--keep-zip",
        action="store_true",
        help="keep the downloaded archive after extraction",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="redownload and re-extract even if the target already exists",
    )
    return parser.parse_args()


def fetch_json(url: str) -> dict:
    with urlopen(url) as response:
        return json.load(response)


def latest_metadata() -> dict:
    return fetch_json(f"{STATIC_DATA_ROOT}latest.jsonl")


def package_name(build_number: int) -> str:
    return f"eve-online-static-data-{build_number}-yaml.zip"


def package_url(build_number: int) -> str:
    return f"{STATIC_DATA_ROOT}{package_name(build_number)}"


def download_file(url: str, destination: Path, force: bool) -> None:
    if destination.exists() and not force:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urlopen(url) as response, destination.open("wb") as handle:
        shutil.copyfileobj(response, handle)


def normalize_region_names(region_names: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in region_names:
        name = value.strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(name)
    return normalized


def should_extract(member_name: str, region_names: list[str]) -> bool:
    basename = PurePosixPath(member_name).name
    if basename in CORE_MAP_FILES:
        return True
    if not region_names:
        return False
    parts = PurePosixPath(member_name).parts
    if len(parts) < 4:
        return False
    lowered = [part.casefold() for part in parts]
    for region_name in region_names:
        region_key = region_name.casefold()
        for index in range(len(lowered) - 3):
            if lowered[index : index + 3] == ["universe", "eve", region_key]:
                return True
            if (
                index + 4 <= len(lowered)
                and lowered[index : index + 4] == ["sde", "universe", "eve", region_key]
            ):
                return True
    return False


def extract_archive(
    archive_path: Path,
    extract_root: Path,
    region_names: list[str],
    force: bool,
) -> None:
    if extract_root.exists() and force:
        shutil.rmtree(extract_root)
    extract_root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        members = [
            member
            for member in archive.infolist()
            if not member.is_dir() and should_extract(member.filename, region_names)
        ]
        if not members:
            raise SystemExit("archive did not contain any map-import files")
        for member in members:
            archive.extract(member, path=extract_root)


def write_metadata(destination: Path, metadata: dict, archive_name: str) -> None:
    payload = {
        "source": "official",
        "static_data_root": STATIC_DATA_ROOT,
        "archive_name": archive_name,
        "build_number": metadata.get("buildNumber"),
        "release_date": metadata.get("releaseDate"),
    }
    destination.write_text(
        json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    args = parse_args()
    if args.build:
        build_number = int(args.build)
        metadata = {
            "buildNumber": build_number,
            "releaseDate": "",
        }
    else:
        metadata = latest_metadata()
        build_number = int(metadata["buildNumber"])
    archive_name = package_name(build_number)
    target_root = Path(args.target).resolve()
    archive_path = target_root / "downloads" / archive_name
    extract_root = target_root / str(build_number)
    marker_path = extract_root / "_metadata.json"
    region_names = normalize_region_names(list(args.region_name))

    if marker_path.exists() and not args.force:
        print(extract_root)
        return 0

    url = package_url(build_number)
    download_file(url, archive_path, force=args.force)
    extract_archive(
        archive_path=archive_path,
        extract_root=extract_root,
        region_names=region_names,
        force=args.force,
    )
    write_metadata(marker_path, metadata, archive_name)

    if not args.keep_zip and archive_path.exists():
        archive_path.unlink()

    print(extract_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
