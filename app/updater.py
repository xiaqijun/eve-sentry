"""Asynchronous portable-client update support."""

from __future__ import annotations

import hashlib
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PyQt6.QtCore import QObject, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkProxy,
    QNetworkProxyFactory,
    QNetworkReply,
    QNetworkRequest,
)

from app.version import current_version, update_manifest_url


logger = logging.getLogger(__name__)
PENDING_UPDATE_FILENAME = "pending-update.json"
UPDATE_RESULT_FILENAME = "update-result.json"
UPDATE_LOG_FILENAME = "update.log"
UPDATE_HEALTH_STABILITY_SECONDS = 8
UPDATE_RESULT_POLL_ATTEMPTS = 15
UPDATE_RESULT_POLL_INTERVAL_MS = 1000
OWNED_EXPANDED_DIRNAME = "EVE-Sentry-Monitor-ONNX"


class UpdateError(RuntimeError):
    """Raised when release metadata or a downloaded package is invalid."""


@dataclass(frozen=True)
class UpdateComponent:
    """One independently downloadable update component."""

    version: str
    url: str
    sha256: str
    size: int
    filename: str


@dataclass(frozen=True)
class ReleaseInfo:
    """Validated fields from one update manifest."""

    version: str
    url: str
    sha256: str
    size: int
    filename: str
    models: UpdateComponent | None = None


class _UpdaterFileTask(QThread):
    """Run one disk-heavy updater operation outside the Qt event loop."""

    succeeded = pyqtSignal(object)
    failed = pyqtSignal(object)

    def __init__(self, task: Callable[[], object], parent: QObject) -> None:
        super().__init__(parent)
        self._task = task

    def run(self) -> None:
        try:
            result = self._task()
        except Exception as exc:
            self.failed.emit(exc)
            return
        self.succeeded.emit(result)


def configured_update_proxy() -> str:
    """Return the explicit proxy URL for update traffic, if configured."""
    for name in (
        "EVE_SENTRY_HTTP_PROXY",
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
    ):
        value = str(os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def network_proxy_from_url(value: str) -> QNetworkProxy | None:
    """Convert an HTTP or SOCKS proxy URL into a Qt network proxy."""
    url = QUrl(str(value or "").strip())
    scheme = url.scheme().casefold()
    if not url.isValid() or not url.host() or scheme not in {
        "http",
        "https",
        "socks5",
        "socks5h",
        "socks",
    }:
        return None
    if scheme in {"socks", "socks5", "socks5h"}:
        proxy_type = QNetworkProxy.ProxyType.Socks5Proxy
        default_port = 1080
    else:
        proxy_type = QNetworkProxy.ProxyType.HttpProxy
        default_port = 8080
    port = url.port(default_port)
    if port <= 0:
        return None
    return QNetworkProxy(
        proxy_type,
        url.host(),
        port,
        url.userName(),
        url.password(),
    )


def configure_update_proxy(network: QNetworkAccessManager) -> str:
    """Apply explicit or Windows system proxy settings to the updater."""
    proxy_url = configured_update_proxy()
    proxy = network_proxy_from_url(proxy_url)
    if proxy is not None:
        network.setProxy(proxy)
        return proxy_url

    # Keep credentials out of the application and let Qt read the user's
    # Windows proxy/PAC configuration when no explicit proxy is configured.
    QNetworkProxyFactory.setUseSystemConfiguration(True)
    return ""


def version_key(value: str) -> tuple[tuple[int, ...], int, str]:
    """Return a sortable key for simple semantic versions."""
    text = str(value or "").strip().removeprefix("v")
    match = re.fullmatch(r"(\d+(?:\.\d+)*)(?:[-+](.+))?", text)
    if match is None:
        raise UpdateError(f"无效版本号：{value}")
    numbers = tuple(int(part) for part in match.group(1).split("."))
    suffix = str(match.group(2) or "")
    return numbers, 1 if not suffix else 0, suffix


def is_newer_version(candidate: str, installed: str) -> bool:
    """Return whether candidate is newer than installed."""
    candidate_key = version_key(candidate)
    installed_key = version_key(installed)
    width = max(len(candidate_key[0]), len(installed_key[0]))
    candidate_numbers = candidate_key[0] + (0,) * (width - len(candidate_key[0]))
    installed_numbers = installed_key[0] + (0,) * (width - len(installed_key[0]))
    return (candidate_numbers, *candidate_key[1:]) > (
        installed_numbers,
        *installed_key[1:],
    )


def parse_release_manifest(payload: Any) -> ReleaseInfo:
    """Validate and normalize release manifest data."""
    if not isinstance(payload, dict):
        raise UpdateError("更新清单格式错误")
    version = str(payload.get("version") or "").strip().removeprefix("v")
    url = str(payload.get("url") or "").strip()
    sha256 = str(payload.get("sha256") or "").strip().lower()
    filename = str(payload.get("filename") or Path(QUrl(url).path()).name).strip()
    try:
        size = int(payload.get("size") or 0)
    except (TypeError, ValueError) as exc:
        raise UpdateError("更新包大小无效") from exc
    version_key(version)
    if not QUrl(url).isValid() or not url.lower().startswith("https://"):
        raise UpdateError("更新包必须使用 HTTPS 地址")
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise UpdateError("更新包校验值无效")
    if size <= 0:
        raise UpdateError("更新包大小无效")
    if not filename or Path(filename).name != filename or not filename.lower().endswith(".zip"):
        raise UpdateError("更新包文件名无效")
    models = None
    components = payload.get("components")
    if isinstance(components, dict) and isinstance(components.get("models"), dict):
        models = _parse_update_component(components["models"], "OCR 模型")
    return ReleaseInfo(version, url, sha256, size, filename, models)


def _parse_update_component(payload: dict[str, Any], label: str) -> UpdateComponent:
    version = str(payload.get("version") or "").strip()
    url = str(payload.get("url") or "").strip()
    sha256 = str(payload.get("sha256") or "").strip().lower()
    filename = str(payload.get("filename") or Path(QUrl(url).path()).name).strip()
    try:
        size = int(payload.get("size") or 0)
    except (TypeError, ValueError) as exc:
        raise UpdateError(f"{label}包大小无效") from exc
    if not version:
        raise UpdateError(f"{label}版本无效")
    if not QUrl(url).isValid() or not url.lower().startswith("https://"):
        raise UpdateError(f"{label}包必须使用 HTTPS 地址")
    if not re.fullmatch(r"[0-9a-f]{64}", sha256) or size <= 0:
        raise UpdateError(f"{label}包校验信息无效")
    if not filename or Path(filename).name != filename or not filename.endswith(".zip"):
        raise UpdateError(f"{label}包文件名无效")
    return UpdateComponent(version, url, sha256, size, filename)


def installed_model_version() -> str:
    """Return the version of the separately installed ONNX model bundle."""
    roots = []
    if getattr(sys, "frozen", False):
        bundle_root = getattr(sys, "_MEIPASS", None)
        if bundle_root:
            roots.append(Path(bundle_root) / "models")
        roots.append(Path(sys.executable).resolve().parent / "models")
    configured = os.environ.get("EVE_SENTRY_ONNX_MODEL_DIR", "").strip()
    if configured:
        roots.append(Path(configured))
    for root in roots:
        try:
            payload = json.loads((root / "version.json").read_text(encoding="utf-8"))
            return str(payload.get("version") or "").strip()
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return ""


def canonical_manifest_bytes(payload: dict[str, Any]) -> bytes:
    """Return the exact canonical bytes covered by an update signature."""
    signed = {
        key: value
        for key, value in payload.items()
        if key not in {"signature", "signature_algorithm", "signing_key_id"}
    }
    return json.dumps(
        signed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def verify_release_manifest_signature(
    payload: dict[str, Any],
    public_key_path: Path,
    *,
    allow_unsigned: bool = False,
) -> None:
    """Verify the Ed25519 signature before trusting hashes or download URLs."""
    signature = str(payload.get("signature") or "").strip()
    if not signature:
        if allow_unsigned:
            return
        raise UpdateError("更新清单缺少数字签名")
    if str(payload.get("signature_algorithm") or "ed25519").casefold() != "ed25519":
        raise UpdateError("更新清单签名算法不受支持")
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        public_key = serialization.load_pem_public_key(
            Path(public_key_path).read_bytes()
        )
        if not isinstance(public_key, Ed25519PublicKey):
            raise UpdateError("更新签名公钥类型无效")
        public_key.verify(
            base64.b64decode(signature, validate=True),
            canonical_manifest_bytes(payload),
        )
    except UpdateError:
        raise
    except (OSError, ValueError, ImportError) as exc:
        raise UpdateError(f"更新清单签名校验失败：{exc}") from exc
    except Exception as exc:
        raise UpdateError("更新清单签名无效") from exc


def default_update_public_key_path() -> Path:
    """Return the signing public key bundled beside application resources."""
    return Path(__file__).resolve().parent.parent / "resources" / "update_public_key.pem"


def default_update_dir() -> Path:
    """Return the per-user directory used for update downloads."""
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "EVE Sentry" / "updates"
    return Path.home() / ".eve-sentry" / "updates"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_size(path: Path) -> int:
    total = 0
    for candidate in Path(path).rglob("*"):
        try:
            if candidate.is_file():
                total += candidate.stat().st_size
        except OSError:
            continue
    return total


def _is_link_or_junction(path: Path) -> bool:
    """Return whether a directory is a link that cleanup must never traverse."""
    is_junction = getattr(os.path, "isjunction", lambda _path: False)
    return path.is_symlink() or bool(is_junction(path))


def _safe_zip_members(path: Path) -> tuple[list[str], int]:
    """Validate archive member paths and return names plus expanded size."""
    names: list[str] = []
    expanded_size = 0
    with zipfile.ZipFile(path) as archive:
        for entry in archive.infolist():
            normalized = entry.filename.replace("\\", "/")
            member = Path(normalized)
            unix_type = (entry.external_attr >> 16) & 0o170000
            if (
                not normalized
                or normalized.startswith("/")
                or member.is_absolute()
                or ".." in member.parts
                or unix_type == 0o120000
            ):
                raise UpdateError(f"更新包包含不安全路径：{entry.filename}")
            names.append(normalized.rstrip("/"))
            expanded_size += max(0, int(entry.file_size))
    return names, expanded_size


def _verify_staged_component(path: Path, component: UpdateComponent) -> None:
    """Revalidate a staged package immediately before starting installation."""
    package = Path(path)
    try:
        actual_size = package.stat().st_size
    except OSError as exc:
        raise UpdateError(f"更新包不存在：{package.name}") from exc
    if package.is_symlink() or actual_size != component.size:
        raise UpdateError(f"更新包大小校验失败：{package.name}")
    if _file_sha256(package) != component.sha256:
        raise UpdateError(f"更新包 SHA256 校验失败：{package.name}")


def validate_install_preflight(
    install_dir: Path,
    package_paths: list[Path],
    executable_name: str = "EVE-Sentry-Monitor.exe",
) -> None:
    """Fail before shutdown when the current directory cannot be updated safely."""
    install_root = Path(install_dir)
    if not install_root.is_dir():
        raise UpdateError("当前客户端目录不存在")
    packages = [Path(path) for path in package_paths if path]
    if not packages:
        raise UpdateError("没有可安装的更新包")

    expanded_bytes = 0
    for index, package in enumerate(packages):
        if not package.is_file() or package.is_symlink():
            raise UpdateError(f"更新包不存在：{package.name}")
        try:
            names, package_expanded_bytes = _safe_zip_members(package)
        except (OSError, zipfile.BadZipFile) as exc:
            raise UpdateError(f"更新包无法读取：{package.name}") from exc
        expanded_bytes += package_expanded_bytes
        if index == 0 and not any(
            Path(name).name.casefold() == executable_name.casefold()
            for name in names
        ):
            raise UpdateError(f"程序更新包缺少 {executable_name}")

    probe = install_root / f".eve-sentry-write-test-{os.getpid()}"
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as exc:
        probe.unlink(missing_ok=True)
        raise UpdateError(f"当前客户端目录不可写：{exc}") from exc

    reservations: dict[tuple[str, str], tuple[Path, int]] = {}

    def reserve(location: Path, size: int) -> None:
        resolved = Path(location).resolve()
        try:
            volume_key = ("device", str(os.stat(resolved).st_dev))
        except OSError:
            volume_key = ("anchor", resolved.anchor.casefold())
        previous = reservations.get(volume_key)
        reservations[volume_key] = (
            resolved,
            max(0, int(size)) + (previous[1] if previous else 0),
        )

    install_bytes = _directory_size(install_root)
    reserve(install_root, expanded_bytes)
    reserve(Path(tempfile.gettempdir()), expanded_bytes)
    reserve(packages[0].parent, install_bytes)
    safety_margin = 256 * 1024 * 1024
    for location, reserved_bytes in reservations.values():
        required = reserved_bytes + safety_margin
        free = shutil.disk_usage(location).free
        if free < required:
            required_gib = required / (1024 ** 3)
            free_gib = free / (1024 ** 3)
            raise UpdateError(
                f"磁盘空间不足，需要约 {required_gib:.1f} GB，当前可用 {free_gib:.1f} GB"
            )


def save_pending_update(
    update_dir: Path,
    release: ReleaseInfo,
    program_path: Path,
    model_path: Path | None,
) -> None:
    """Persist a verified download so a restart does not download it again."""
    program_path = Path(program_path)
    model_path = Path(model_path) if model_path is not None else None
    payload = {
        "version": release.version,
        "program": {
            "filename": program_path.name,
            "sha256": release.sha256,
            "size": release.size,
        },
        "models": None,
    }
    if release.models is not None and model_path is not None:
        payload["models"] = {
            "version": release.models.version,
            "filename": model_path.name,
            "sha256": release.models.sha256,
            "size": release.models.size,
        }
    root = Path(update_dir)
    root.mkdir(parents=True, exist_ok=True)
    destination = root / PENDING_UPDATE_FILENAME
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(destination)


def load_pending_update(
    update_dir: Path,
    installed_version: str,
    *,
    allow_current_version: bool = False,
) -> tuple[ReleaseInfo, Path, Path | None] | None:
    """Restore locally verified packages when they still match persisted hashes."""
    root = Path(update_dir)
    state_path = root / PENDING_UPDATE_FILENAME
    if not state_path.is_file() or state_path.is_symlink():
        return None
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        version = str(payload["version"])
        is_newer = is_newer_version(version, installed_version)
        is_current = not is_newer and not is_newer_version(
            installed_version,
            version,
        )
        if not is_newer and not (allow_current_version and is_current):
            try:
                state_path.unlink(missing_ok=True)
            except OSError:
                pass
            return None
        program = payload["program"]
        program_filename = str(program["filename"])
        if Path(program_filename).name != program_filename:
            raise UpdateError("已下载程序包路径无效")
        program_path = root / program_filename
        program_component = UpdateComponent(
            version,
            "https://local.invalid/program",
            str(program["sha256"]),
            int(program["size"]),
            program_path.name,
        )
        if (
            not program_path.is_file()
            or program_path.is_symlink()
            or program_path.stat().st_size != program_component.size
            or _file_sha256(program_path) != program_component.sha256
        ):
            raise UpdateError("已下载程序包校验失败")
        model_component = None
        model_path = None
        model = payload.get("models")
        if isinstance(model, dict):
            model_filename = str(model["filename"])
            if Path(model_filename).name != model_filename:
                raise UpdateError("已下载模型包路径无效")
            model_path = root / model_filename
            model_component = UpdateComponent(
                str(model["version"]),
                "https://local.invalid/models",
                str(model["sha256"]),
                int(model["size"]),
                model_path.name,
            )
            if (
                not model_path.is_file()
                or model_path.is_symlink()
                or model_path.stat().st_size != model_component.size
                or _file_sha256(model_path) != model_component.sha256
            ):
                raise UpdateError("已下载模型包校验失败")
        return (
            ReleaseInfo(
                version,
                "https://local.invalid/program",
                program_component.sha256,
                program_component.size,
                program_component.filename,
                model_component,
            ),
            program_path,
            model_path,
        )
    except (KeyError, TypeError, ValueError, OSError, UpdateError, json.JSONDecodeError):
        logger.warning("Discarding invalid pending client update", exc_info=True)
        try:
            state_path.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def load_update_result(update_dir: Path) -> dict[str, str] | None:
    """Consume the previous detached installer's user-visible result."""
    path = Path(update_dir) / UPDATE_RESULT_FILENAME
    if not path.is_file() or path.is_symlink():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        status = str(payload.get("status") or "")
        version = str(payload.get("version") or "")
        message = str(payload.get("message") or "")
        if status not in {"success", "rolled_back", "failed"}:
            return None
        return {"status": status, "version": version, "message": message}
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def cleanup_update_artifacts(
    update_dir: Path,
    temp_dir: Path | None = None,
    stale_after_seconds: float = 7 * 24 * 60 * 60,
    preserved_paths: tuple[Path, ...] = (),
) -> None:
    """Remove packages and staging files left by completed or interrupted updates."""
    update_root = Path(update_dir)
    preserved = {Path(path).resolve() for path in preserved_paths}
    stale_before = time.time() - max(0.0, float(stale_after_seconds))
    for pattern in (
        "EVE-Sentry-Monitor-*.zip",
        "EVE-Sentry-Monitor-*.zip.part",
        "apply-*.ps1",
    ):
        for path in update_root.glob(pattern):
            try:
                if (
                    (path.is_file() or path.is_symlink())
                    and path.resolve() not in preserved
                    and path.stat().st_mtime <= stale_before
                ):
                    path.unlink()
            except OSError:
                continue
    expanded_path = update_root / OWNED_EXPANDED_DIRNAME
    try:
        if expanded_path.is_dir() and not _is_link_or_junction(expanded_path):
            shutil.rmtree(expanded_path)
    except OSError:
        pass
    try:
        update_root.rmdir()
    except OSError:
        pass

    stage_root = Path(temp_dir or tempfile.gettempdir())
    for path in stage_root.glob("eve-sentry-update-*"):
        if not path.is_dir() or _is_link_or_junction(path):
            continue
        try:
            if path.stat().st_mtime > stale_before:
                continue
            shutil.rmtree(path)
        except OSError:
            continue


def build_update_script(
    package_path: Path,
    install_dir: Path,
    executable_name: str,
    process_id: int,
    model_package_path: Path | None = None,
    target_version: str = "",
    package_sha256: str = "",
    package_size: int = 0,
    model_sha256: str = "",
    model_size: int = 0,
) -> str:
    """Build the PowerShell script that replaces files after client exit."""
    values = {
        "package": str(package_path),
        "install": str(install_dir),
        "exe": executable_name,
        "models": str(model_package_path or ""),
        "version": str(target_version),
        "package_hash": str(package_sha256).lower(),
        "model_hash": str(model_sha256).lower(),
    }
    escaped = {key: value.replace("'", "''") for key, value in values.items()}
    return f"""$ErrorActionPreference = 'Stop'
$package = '{escaped['package']}'
$install = '{escaped['install']}'
$modelPackage = '{escaped['models']}'
$targetVersion = '{escaped['version']}'
$executableName = '{escaped['exe']}'
$expectedPackageHash = '{escaped['package_hash']}'
$expectedPackageSize = [int64]{max(0, int(package_size))}
$expectedModelHash = '{escaped['model_hash']}'
$expectedModelSize = [int64]{max(0, int(model_size))}
$updateRoot = Split-Path -Parent $PSCommandPath
$stage = Join-Path ([IO.Path]::GetTempPath()) ('eve-sentry-update-' + [guid]::NewGuid())
$backup = Join-Path $updateRoot 'previous-version'
$healthMarker = Join-Path $updateRoot 'startup-ok.marker'
$resultPath = Join-Path $updateRoot '{UPDATE_RESULT_FILENAME}'
$logPath = Join-Path $updateRoot '{UPDATE_LOG_FILENAME}'
$backupComplete = $false
$installStarted = $false
$updated = $false
$rollbackSucceeded = $false
$newProcess = $null
function Write-UpdateLog([string]$message) {{
    try {{
        $line = "$(Get-Date -Format o) $message"
        Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
    }} catch {{}}
}}
function Write-UpdateResult([string]$status, [string]$message) {{
    try {{
        $temporaryResult = "$resultPath.tmp"
        @{{ status = $status; version = $targetVersion; message = $message; completed_at = [DateTimeOffset]::UtcNow.ToString('o') }} |
            ConvertTo-Json | Set-Content -LiteralPath $temporaryResult -Encoding UTF8
        Move-Item -LiteralPath $temporaryResult -Destination $resultPath -Force
    }} catch {{}}
}}
function Start-InstalledClient([bool]$withHealthMarker) {{
    $executable = Join-Path $install $executableName
    if ($withHealthMarker) {{
        $quotedMarker = '"' + $healthMarker.Replace('"', '\"') + '"'
        return Start-Process -FilePath $executable -WorkingDirectory $install -ArgumentList @('--update-health-marker', $quotedMarker) -PassThru
    }}
    Start-Process -FilePath $executable -WorkingDirectory $install | Out-Null
}}
try {{
    New-Item -ItemType Directory -Path $updateRoot -Force | Out-Null
    Write-UpdateLog "install started for version $targetVersion"
    if (-not (Test-Path -LiteralPath $package -PathType Leaf)) {{ throw 'program package is missing' }}
    if (-not (Test-Path -LiteralPath $install -PathType Container)) {{ throw 'install directory is missing' }}
    $writeProbe = Join-Path $install ('.eve-sentry-write-test-' + $PID)
    Set-Content -LiteralPath $writeProbe -Value 'ok' -Encoding ASCII
    Remove-Item -LiteralPath $writeProbe -Force
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($package)
    try {{
        $hasExecutable = @($archive.Entries | Where-Object {{ [IO.Path]::GetFileName($_.FullName) -ieq $executableName }}).Count -gt 0
        if (-not $hasExecutable) {{ throw "program package does not contain $executableName" }}
    }} finally {{
        $archive.Dispose()
    }}
    Wait-Process -Id {int(process_id)} -ErrorAction SilentlyContinue
    Write-UpdateLog 'previous client exited'
    if ($expectedPackageSize -gt 0 -and (Get-Item -LiteralPath $package).Length -ne $expectedPackageSize) {{
        throw 'program package size changed before installation'
    }}
    if ($expectedPackageHash -and (Get-FileHash -LiteralPath $package -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedPackageHash) {{
        throw 'program package hash changed before installation'
    }}
    if ($modelPackage) {{
        if ($expectedModelSize -gt 0 -and (Get-Item -LiteralPath $modelPackage).Length -ne $expectedModelSize) {{
            throw 'model package size changed before installation'
        }}
        if ($expectedModelHash -and (Get-FileHash -LiteralPath $modelPackage -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedModelHash) {{
            throw 'model package hash changed before installation'
        }}
    }}
    Expand-Archive -LiteralPath $package -DestinationPath $stage -Force
    $source = Join-Path $stage '{OWNED_EXPANDED_DIRNAME}'
    if (-not (Test-Path -LiteralPath $source)) {{
        $directories = @(Get-ChildItem -LiteralPath $stage -Directory)
        if ($directories.Count -eq 1) {{ $source = $directories[0].FullName }} else {{ $source = $stage }}
    }}
    if (-not (Test-Path -LiteralPath (Join-Path $source $executableName) -PathType Leaf)) {{
        throw "expanded package does not contain $executableName at its root"
    }}
    if ($modelPackage) {{
        if (-not (Test-Path -LiteralPath $modelPackage -PathType Leaf)) {{ throw 'model package is missing' }}
        $modelStage = Join-Path $stage 'model-component'
        Expand-Archive -LiteralPath $modelPackage -DestinationPath $modelStage -Force
        $modelSource = Join-Path $modelStage 'models'
        if (-not (Test-Path -LiteralPath $modelSource)) {{ $modelSource = $modelStage }}
        $internalRoot = Join-Path $source '_internal'
        $targetModels = if (Test-Path -LiteralPath $internalRoot) {{ Join-Path $internalRoot 'models' }} else {{ Join-Path $source 'models' }}
        New-Item -ItemType Directory -Path $targetModels -Force | Out-Null
        $null = & robocopy $modelSource $targetModels /MIR /R:2 /W:1
        if ($LASTEXITCODE -ge 8) {{ throw "model staging failed with exit code $LASTEXITCODE" }}
    }}
    Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $backup -Force | Out-Null
    $null = & robocopy $install $backup /MIR /R:2 /W:1
    if ($LASTEXITCODE -ge 8) {{ throw "backup failed with exit code $LASTEXITCODE" }}
    $backupComplete = $true
    Write-UpdateLog 'backup completed'
    $installCopyArgs = @('/XF', 'region_prefs.json')
    if (-not $modelPackage) {{ $installCopyArgs += @('/XD', 'models') }}
    $installStarted = $true
    $null = & robocopy $source $install /MIR /R:3 /W:1 @installCopyArgs
    if ($LASTEXITCODE -ge 8) {{ throw "install copy failed with exit code $LASTEXITCODE" }}
    Write-UpdateLog 'new files installed'
    Remove-Item -LiteralPath $healthMarker -Force -ErrorAction SilentlyContinue
    $newProcess = Start-InstalledClient $true
    Write-UpdateLog 'new client launched; waiting for {UPDATE_HEALTH_STABILITY_SECONDS}-second stability confirmation'
    for ($attempt = 0; $attempt -lt 45; $attempt++) {{
        if ($newProcess.HasExited) {{ break }}
        if (Test-Path -LiteralPath $healthMarker -PathType Leaf) {{
            $confirmedVersion = (Get-Content -LiteralPath $healthMarker -Raw -ErrorAction SilentlyContinue).Trim()
            if ($confirmedVersion -eq $targetVersion) {{ $updated = $true; break }}
        }}
        Start-Sleep -Seconds 1
    }}
    if (-not $updated) {{ throw 'updated client did not remain healthy or confirm its version' }}
    Write-UpdateLog 'startup health check passed'
    Write-UpdateResult 'success' '客户端更新成功'
}} catch {{
    $failure = $_.Exception.Message
    Write-UpdateLog "install failed: $failure"
    if ($null -ne $newProcess -and -not $newProcess.HasExited) {{
        Stop-Process -Id $newProcess.Id -Force -ErrorAction SilentlyContinue
    }}
    if ($backupComplete -and $installStarted) {{
        $null = & robocopy $backup $install /MIR /R:3 /W:1
        if ($LASTEXITCODE -lt 8) {{
            $rollbackSucceeded = $true
            Write-UpdateLog 'previous version restored'
            Write-UpdateResult 'rolled_back' "更新失败，已恢复旧版本：$failure"
            try {{ Start-InstalledClient $false }} catch {{ Write-UpdateLog "restored client could not start: $($_.Exception.Message)" }}
        }} else {{
            Write-UpdateLog "rollback failed with exit code $LASTEXITCODE"
            Write-UpdateResult 'failed' "更新和回滚均失败，备份已保留：$failure"
        }}
    }} else {{
        Write-UpdateResult 'failed' "更新未开始，原版本未改动：$failure"
        if (Test-Path -LiteralPath (Join-Path $install $executableName) -PathType Leaf) {{
            try {{ Start-InstalledClient $false }} catch {{ Write-UpdateLog "existing client could not start: $($_.Exception.Message)" }}
        }}
    }}
}} finally {{
    Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $healthMarker -Force -ErrorAction SilentlyContinue
    if ($updated) {{
        Remove-Item -LiteralPath $package -Force -ErrorAction SilentlyContinue
        if ($modelPackage) {{ Remove-Item -LiteralPath $modelPackage -Force -ErrorAction SilentlyContinue }}
        Remove-Item -LiteralPath (Join-Path $updateRoot '{PENDING_UPDATE_FILENAME}') -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue
    }} elseif ($rollbackSucceeded) {{
        Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue
    }}
    Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
}}
"""


class ClientUpdater(QObject):
    """Check, download, verify, and stage portable client releases."""

    state_changed = pyqtSignal(str, str, bool)
    restart_requested = pyqtSignal()

    def __init__(
        self,
        parent: QObject | None = None,
        manifest_url: str | None = None,
        installed_version: str | None = None,
        update_dir: Path | None = None,
        public_key_path: Path | None = None,
        background_download: bool = True,
        asynchronous_file_tasks: bool | None = None,
    ) -> None:
        super().__init__(parent)
        self._asynchronous_file_tasks = (
            parent is not None
            if asynchronous_file_tasks is None
            else bool(asynchronous_file_tasks)
        )
        self.manifest_url = str(manifest_url or update_manifest_url()).strip()
        self.installed_version = str(installed_version or current_version()).strip()
        self.update_dir = Path(update_dir or default_update_dir())
        self.public_key_path = Path(
            public_key_path or default_update_public_key_path()
        )
        self.allow_unsigned = (
            os.environ.get("EVE_SENTRY_ALLOW_UNSIGNED_UPDATES", "0")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"}
        )
        self.background_download = bool(background_download)
        self._network = QNetworkAccessManager(self)
        self._proxy_url = configure_update_proxy(self._network)
        self._release: ReleaseInfo | None = None
        self._download_reply: QNetworkReply | None = None
        self._download_file = None
        self._download_hash = hashlib.sha256()
        self._download_path: Path | None = None
        self._ready_path: Path | None = None
        self._program_ready_path: Path | None = None
        self._model_ready_path: Path | None = None
        self._current_component: UpdateComponent | None = None
        self._download_kind = "program"
        self._resume_offset = 0
        self._installer_launched = False
        self._busy = False
        self._restoring = True
        self._preflight_ready = False
        self._preflight_signatures: dict[Path, tuple[int, int]] = {}
        self._preflight_after_restore = False
        self._pending_check_manual: bool | None = None
        self._file_tasks: set[_UpdaterFileTask] = set()
        self._result_poll_attempt = 0
        self._run_file_task(
            self._load_local_update_state,
            self._finish_local_update_state,
            self._fail_local_update_state,
        )

    def _run_file_task(
        self,
        task: Callable[[], object],
        on_success: Callable[[object], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        if not self._asynchronous_file_tasks:
            try:
                on_success(task())
            except Exception as exc:
                on_error(exc)
            return
        worker = _UpdaterFileTask(task, self)
        self._file_tasks.add(worker)
        worker.succeeded.connect(on_success)
        worker.failed.connect(on_error)
        worker.finished.connect(
            lambda worker=worker: self._release_file_task(worker)
        )
        worker.start()

    def _release_file_task(self, worker: _UpdaterFileTask) -> None:
        self._file_tasks.discard(worker)
        worker.deleteLater()

    def _load_local_update_state(self) -> tuple[ReleaseInfo, Path, Path | None] | None:
        installer_in_flight = any(self.update_dir.glob("apply-*.ps1"))
        pending = load_pending_update(
            self.update_dir,
            self.installed_version,
            allow_current_version=installer_in_flight,
        )
        preserved_paths = tuple(pending[1:] if pending is not None else ())
        cleanup_update_artifacts(
            self.update_dir,
            preserved_paths=tuple(
                path for path in preserved_paths if path is not None
            ),
        )
        if pending is None or not is_newer_version(
            pending[0].version,
            self.installed_version,
        ):
            return None
        return pending

    def _finish_local_update_state(self, pending: object) -> None:
        restored = pending if isinstance(pending, tuple) else None
        if restored is not None:
            self._release = restored[0]
            self._ready_path = restored[1]
            self._program_ready_path = restored[1]
            self._model_ready_path = restored[2]
        self._restoring = False
        if restored is not None and self._asynchronous_file_tasks:
            self._prepare_ready_update(after_restore=True)
        else:
            self._emit_initial_update_state()
            pending_manual = self._pending_check_manual
            self._pending_check_manual = None
            if pending_manual is not None:
                self.check(manual=pending_manual)

    def _fail_local_update_state(self, exc: Exception) -> None:
        logger.warning("Could not restore local update state: %s", exc)
        self._restoring = False
        self._emit_initial_update_state()
        pending_manual = self._pending_check_manual
        self._pending_check_manual = None
        if pending_manual is not None:
            self.check(manual=pending_manual)

    @property
    def ready_to_install(self) -> bool:
        """Return whether a verified update is waiting for client shutdown."""
        preflight_ready = (
            not self._asynchronous_file_tasks or self._preflight_ready
        )
        return (
            self._ready_path is not None
            and not self._installer_launched
            and preflight_ready
        )

    @property
    def has_running_file_tasks(self) -> bool:
        return any(worker.isRunning() for worker in self._file_tasks)

    def _emit_initial_update_state(self) -> None:
        """Expose restored downloads and detached installer results after wiring UI."""
        result = load_update_result(self.update_dir)
        if result is not None:
            status = result["status"]
            version = result["version"]
            message = result["message"]
            if status == "success":
                self.state_changed.emit(
                    message or f"已更新到 v{version}",
                    "检查更新",
                    True,
                )
            elif self.ready_to_install:
                self.state_changed.emit(
                    message or "更新失败，已恢复旧版本",
                    "重试安装",
                    True,
                )
            else:
                self.state_changed.emit(
                    message or "更新安装失败，请查看 update.log",
                    "检查更新",
                    True,
                )
            return
        if self.ready_to_install and self._result_poll_attempt == 0:
            version = self._release.version if self._release is not None else ""
            self.state_changed.emit(
                f"v{version} 更新包已恢复，退出时自动安装",
                "立即安装",
                True,
            )
        if (
            self._result_poll_attempt < UPDATE_RESULT_POLL_ATTEMPTS
            and any(self.update_dir.glob("apply-*.ps1"))
        ):
            self._result_poll_attempt += 1
            QTimer.singleShot(
                UPDATE_RESULT_POLL_INTERVAL_MS,
                self._emit_initial_update_state,
            )

    def request_action(self) -> None:
        """Perform the action represented by the current update state."""
        if self._busy:
            return
        if self._ready_path is not None:
            self.install_and_restart()
        elif self._release is not None:
            self.download()
        else:
            self.check(manual=True)

    def check(self, manual: bool = False) -> None:
        """Fetch the latest release manifest without blocking the UI."""
        if self._restoring:
            self._pending_check_manual = bool(manual)
            return
        if self._busy:
            return
        url = QUrl(self.manifest_url)
        if not url.isValid() or url.scheme().lower() != "https":
            if manual:
                self.state_changed.emit("更新地址无效", "重试", True)
            return
        self._busy = True
        self.state_changed.emit("正在检查更新", "检查中", False)
        request = self._request(url)
        reply = self._network.get(request)
        reply.finished.connect(lambda: self._finish_check(reply, manual))

    def download(self) -> None:
        """Download the selected release and stream it to per-user storage."""
        release = self._release
        if self._busy or release is None:
            return
        if self._program_ready_path is not None and release.models is not None:
            self._begin_component_download(release.models, "models")
            return
        component = UpdateComponent(
            release.version,
            release.url,
            release.sha256,
            release.size,
            release.filename,
        )
        self._begin_component_download(component, "program")

    def _begin_component_download(
        self,
        component: UpdateComponent,
        kind: str,
    ) -> None:
        self.update_dir.mkdir(parents=True, exist_ok=True)
        target = self.update_dir / component.filename
        partial = target.with_suffix(target.suffix + ".part")
        self._busy = True
        self._current_component = component
        self._download_kind = str(kind)
        self.state_changed.emit("正在校验下载断点", "准备下载", False)
        self._run_file_task(
            lambda: self._load_partial_download_state(partial, component.size),
            lambda result: self._start_component_download(
                component,
                kind,
                target,
                partial,
                result,
            ),
            self._fail_component_download_prepare,
        )

    @staticmethod
    def _load_partial_download_state(
        partial: Path,
        expected_size: int,
    ) -> tuple[int, object]:
        if partial.exists() and partial.stat().st_size > expected_size:
            partial.unlink(missing_ok=True)
        resume_offset = partial.stat().st_size if partial.exists() else 0
        digest = hashlib.sha256()
        if resume_offset:
            with partial.open("rb") as existing:
                for chunk in iter(lambda: existing.read(1024 * 1024), b""):
                    digest.update(chunk)
        return resume_offset, digest

    def _start_component_download(
        self,
        component: UpdateComponent,
        kind: str,
        target: Path,
        partial: Path,
        prepared: object,
    ) -> None:
        resume_offset, digest = prepared
        try:
            file_handle = partial.open("ab")
        except OSError as exc:
            self._busy = False
            self.state_changed.emit(f"无法保存更新包：{exc}", "重试", True)
            return
        self._resume_offset = int(resume_offset)
        self._current_component = component
        self._download_kind = str(kind)
        self._download_path = partial
        self._download_file = file_handle
        self._download_hash = digest
        initial_percent = round(self._resume_offset * 100 / component.size)
        self.state_changed.emit(
            f"正在下载 {initial_percent}%",
            "下载中",
            False,
        )
        request = self._request(QUrl(component.url), timeout_ms=60000)
        if self._resume_offset:
            request.setRawHeader(
                b"Range",
                f"bytes={self._resume_offset}-".encode("ascii"),
            )
        reply = self._network.get(request)
        self._download_reply = reply
        reply.readyRead.connect(self._read_download_data)
        reply.downloadProgress.connect(self._on_download_progress)
        reply.finished.connect(lambda: self._finish_download(reply, target))

    def _fail_component_download_prepare(self, exc: Exception) -> None:
        self._busy = False
        self.state_changed.emit(f"准备下载失败：{exc}", "重试", True)

    def _prepare_ready_update(self, *, after_restore: bool = False) -> None:
        if self._busy:
            return
        package = self._ready_path
        release = self._release
        if package is None or release is None:
            return
        self._busy = True
        self._preflight_ready = False
        self.state_changed.emit("正在校验更新包", "校验中", False)
        self._run_file_task(
            self._validate_ready_update_files,
            lambda result: self._finish_ready_update_preflight(
                result,
                after_restore=after_restore,
            ),
            lambda exc: self._fail_ready_update_preflight(
                exc,
                after_restore=after_restore,
            ),
        )

    def _validate_ready_update_files(self) -> dict[Path, tuple[int, int]]:
        package = self._ready_path
        release = self._release
        if package is None or release is None:
            raise UpdateError("更新包状态不完整")
        program_component = UpdateComponent(
            release.version,
            release.url,
            release.sha256,
            release.size,
            release.filename,
        )
        _verify_staged_component(package, program_component)
        package_paths = [package]
        if self._model_ready_path is not None:
            if release.models is None:
                raise UpdateError("模型更新包状态不完整")
            _verify_staged_component(self._model_ready_path, release.models)
            package_paths.append(self._model_ready_path)
        if sys.platform == "win32" and getattr(sys, "frozen", False):
            install_dir = Path(sys.executable).resolve().parent
            validate_install_preflight(
                install_dir,
                package_paths,
                Path(sys.executable).name,
            )
        return {
            path: (path.stat().st_size, path.stat().st_mtime_ns)
            for path in package_paths
        }

    def _finish_ready_update_preflight(
        self,
        result: object,
        *,
        after_restore: bool,
    ) -> None:
        self._busy = False
        self._preflight_signatures = dict(result)
        self._preflight_ready = True
        if after_restore:
            self._emit_initial_update_state()
            pending_manual = self._pending_check_manual
            self._pending_check_manual = None
            if pending_manual is not None:
                self.check(manual=pending_manual)
            return
        self.state_changed.emit(
            "更新包已就绪，退出时自动安装",
            "立即安装",
            True,
        )

    def _fail_ready_update_preflight(
        self,
        exc: Exception,
        *,
        after_restore: bool,
    ) -> None:
        self._busy = False
        self._preflight_ready = False
        self.state_changed.emit(f"安装前检查失败：{exc}", "重试安装", True)
        if after_restore:
            pending_manual = self._pending_check_manual
            self._pending_check_manual = None
            if pending_manual is not None:
                self.check(manual=pending_manual)

    def install_and_restart(self) -> None:
        """Launch the detached updater and ask the application to exit."""
        if self._launch_installer():
            self.restart_requested.emit()

    def install_on_exit(self) -> bool:
        """Launch a ready update after the application has chosen to exit."""
        return self._launch_installer()

    def _launch_installer(self) -> bool:
        package = self._ready_path
        release = self._release
        if package is None or release is None or self._installer_launched:
            return False
        asynchronous_preflight = self._asynchronous_file_tasks
        if asynchronous_preflight and not self._preflight_ready:
            self._prepare_ready_update()
            return False
        if asynchronous_preflight and not self._ready_file_signatures_match():
            self._preflight_ready = False
            self._prepare_ready_update()
            return False
        if not (sys.platform == "win32" and getattr(sys, "frozen", False)):
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(package.parent)))
            self.state_changed.emit("已打开更新包位置", "检查更新", True)
            return False
        install_dir = Path(sys.executable).resolve().parent
        executable_name = Path(sys.executable).name
        package_paths = [package]
        if self._model_ready_path is not None:
            package_paths.append(self._model_ready_path)
        script_path = self.update_dir / f"apply-{release.version}.ps1"
        try:
            if not asynchronous_preflight:
                program_component = UpdateComponent(
                    release.version,
                    release.url,
                    release.sha256,
                    release.size,
                    release.filename,
                )
                _verify_staged_component(package, program_component)
                if self._model_ready_path is not None:
                    if release.models is None:
                        raise UpdateError("模型更新包状态不完整")
                    _verify_staged_component(self._model_ready_path, release.models)
                validate_install_preflight(
                    install_dir,
                    package_paths,
                    executable_name,
                )
            script_path.write_text(
                build_update_script(
                    package,
                    install_dir,
                    executable_name,
                    os.getpid(),
                    self._model_ready_path,
                    target_version=release.version,
                    package_sha256=release.sha256,
                    package_size=release.size,
                    model_sha256=(release.models.sha256 if release.models else ""),
                    model_size=(release.models.size if release.models else 0),
                ),
                encoding="utf-8-sig",
            )
            subprocess.Popen(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-WindowStyle",
                    "Hidden",
                    "-File",
                    str(script_path),
                ],
                cwd=str(self.update_dir),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                close_fds=True,
            )
        except (OSError, UpdateError, zipfile.BadZipFile) as exc:
            script_path.unlink(missing_ok=True)
            self.state_changed.emit(
                f"安装前检查失败：{exc}",
                "重试安装",
                True,
            )
            return False
        self._installer_launched = True
        self.state_changed.emit("正在安装更新", "安装中", False)
        return True

    def _ready_file_signatures_match(self) -> bool:
        signatures = self._preflight_signatures
        if not signatures:
            return False
        try:
            return all(
                (path.stat().st_size, path.stat().st_mtime_ns) == signature
                for path, signature in signatures.items()
            )
        except OSError:
            return False

    def _request(self, url: QUrl, timeout_ms: int = 15000) -> QNetworkRequest:
        request = QNetworkRequest(url)
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
        )
        request.setTransferTimeout(timeout_ms)
        request.setRawHeader(b"User-Agent", b"EVE-Sentry-Updater/1.0")
        return request

    def _finish_check(self, reply: QNetworkReply, manual: bool) -> None:
        self._busy = False
        try:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                raise UpdateError(reply.errorString())
            payload = json.loads(bytes(reply.readAll()).decode("utf-8-sig"))
            verify_release_manifest_signature(
                payload,
                self.public_key_path,
                allow_unsigned=self.allow_unsigned,
            )
            release = parse_release_manifest(payload)
            if is_newer_version(release.version, self.installed_version):
                self._release = release
                self.state_changed.emit(
                    f"发现新版本 v{release.version}",
                    "下载更新",
                    True,
                )
                if self.background_download and not manual:
                    QTimer.singleShot(0, self.download)
            else:
                self._release = None
                self.state_changed.emit(
                    f"已是最新 v{self.installed_version}",
                    "检查更新",
                    True,
                )
        except (UpdateError, OSError, ValueError, json.JSONDecodeError) as exc:
            if manual:
                self.state_changed.emit(f"检查失败：{exc}", "重试", True)
            else:
                self.state_changed.emit(
                    f"当前版本 v{self.installed_version}",
                    "检查更新",
                    True,
                )
        finally:
            reply.deleteLater()

    def _read_download_data(self) -> None:
        if self._download_reply is None or self._download_file is None:
            return
        if self._resume_offset:
            status = self._download_reply.attribute(
                QNetworkRequest.Attribute.HttpStatusCodeAttribute
            )
            if status is None:
                return
            if status is not None and int(status) != 206:
                self._download_file.seek(0)
                self._download_file.truncate()
                self._download_hash = hashlib.sha256()
                self._resume_offset = 0
        data = bytes(self._download_reply.readAll())
        if data:
            self._download_file.write(data)
            self._download_hash.update(data)

    def _on_download_progress(self, received: int, total: int) -> None:
        component = self._current_component
        if component is not None and component.size > 0:
            current = self._resume_offset + max(0, received)
            percent = max(0, min(100, round(current * 100 / component.size)))
            label = "OCR 模型" if self._download_kind == "models" else "程序"
            self.state_changed.emit(
                f"正在下载{label} {percent}%",
                "下载中",
                False,
            )

    def _finish_download(self, reply: QNetworkReply, target: Path) -> None:
        self._read_download_data()
        self._busy = False
        file_handle = self._download_file
        self._download_file = None
        if file_handle is not None:
            file_handle.close()
        partial = self._download_path
        self._download_reply = None
        self._download_path = None
        release = self._release
        component = self._current_component
        try:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                raise UpdateError(reply.errorString())
            if partial is None or release is None or component is None:
                raise UpdateError("更新下载状态丢失")
            actual_size = partial.stat().st_size
            if actual_size != component.size:
                raise UpdateError(
                    f"文件大小不匹配（{actual_size}/{component.size}）"
                )
            digest = self._download_hash.hexdigest()
            if digest != component.sha256:
                raise UpdateError("SHA256 校验失败")
            partial.replace(target)
            if self._download_kind == "program":
                self._program_ready_path = target
                models = release.models
                if models is not None and models.version != installed_model_version():
                    self._begin_component_download(models, "models")
                    return
                self._ready_path = target
            else:
                self._model_ready_path = target
                self._ready_path = self._program_ready_path
            if self._ready_path is None:
                raise UpdateError("程序更新包状态丢失")
            try:
                save_pending_update(
                    self.update_dir,
                    release,
                    self._ready_path,
                    self._model_ready_path,
                )
            except OSError:
                logger.exception("Could not persist verified pending update")
            if self._asynchronous_file_tasks:
                QTimer.singleShot(0, self._prepare_ready_update)
            else:
                self.state_changed.emit(
                    "更新包已就绪，退出时自动安装",
                    "立即安装",
                    True,
                )
        except (OSError, UpdateError) as exc:
            transport_failed = (
                reply.error() != QNetworkReply.NetworkError.NoError
            )
            if partial is not None and not transport_failed:
                partial.unlink(missing_ok=True)
            action = "继续下载" if transport_failed else "重试"
            self.state_changed.emit(f"下载失败：{exc}", action, True)
        finally:
            reply.deleteLater()
