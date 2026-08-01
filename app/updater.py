"""Asynchronous portable-client update support."""

from __future__ import annotations

import hashlib
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkProxy,
    QNetworkProxyFactory,
    QNetworkReply,
    QNetworkRequest,
)

from app.version import current_version, update_manifest_url


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


def cleanup_update_artifacts(
    update_dir: Path,
    temp_dir: Path | None = None,
    stale_after_seconds: float = 7 * 24 * 60 * 60,
) -> None:
    """Remove packages and staging files left by completed or interrupted updates."""
    update_root = Path(update_dir)
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
                    and path.stat().st_mtime <= stale_before
                ):
                    path.unlink()
            except OSError:
                continue
    try:
        update_root.rmdir()
    except OSError:
        pass

    stage_root = Path(temp_dir or tempfile.gettempdir())
    for path in stage_root.glob("eve-sentry-update-*"):
        if not path.is_dir():
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
) -> str:
    """Build the PowerShell script that replaces files after client exit."""
    values = {
        "package": str(package_path),
        "install": str(install_dir),
        "exe": executable_name,
        "models": str(model_package_path or ""),
    }
    escaped = {key: value.replace("'", "''") for key, value in values.items()}
    return f"""$ErrorActionPreference = 'Stop'
$package = '{escaped['package']}'
$install = '{escaped['install']}'
$modelPackage = '{escaped['models']}'
$updateRoot = Split-Path -Parent $PSCommandPath
$stage = Join-Path ([IO.Path]::GetTempPath()) ('eve-sentry-update-' + [guid]::NewGuid())
$backup = Join-Path $updateRoot 'previous-version'
$healthMarker = Join-Path $updateRoot 'startup-ok.marker'
$updated = $false
try {{
    Wait-Process -Id {int(process_id)} -ErrorAction SilentlyContinue
    Expand-Archive -LiteralPath $package -DestinationPath $stage -Force
    $source = Join-Path $stage 'EVE-Sentry-Monitor-ONNX'
    if (-not (Test-Path -LiteralPath $source)) {{
        $directories = @(Get-ChildItem -LiteralPath $stage -Directory)
        if ($directories.Count -eq 1) {{ $source = $directories[0].FullName }} else {{ $source = $stage }}
    }}
    if ($modelPackage -and (Test-Path -LiteralPath $modelPackage)) {{
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
    $modelCopyArgs = @()
    if (-not $modelPackage) {{ $modelCopyArgs = @('/XD', 'models') }}
    $null = & robocopy $source $install /MIR /R:3 /W:1 @modelCopyArgs
    if ($LASTEXITCODE -ge 8) {{ throw "robocopy failed with exit code $LASTEXITCODE" }}
    Remove-Item -LiteralPath $healthMarker -Force -ErrorAction SilentlyContinue
    Start-Process -FilePath (Join-Path $install '{escaped['exe']}') -WorkingDirectory $install -ArgumentList @('--update-health-marker', $healthMarker)
    for ($attempt = 0; $attempt -lt 30; $attempt++) {{
        if (Test-Path -LiteralPath $healthMarker) {{ $updated = $true; break }}
        Start-Sleep -Seconds 1
    }}
    if (-not $updated) {{
        Get-Process -Name ([IO.Path]::GetFileNameWithoutExtension('{escaped['exe']}')) -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        $null = & robocopy $backup $install /MIR /R:3 /W:1
        if ($LASTEXITCODE -ge 8) {{ throw "rollback failed with exit code $LASTEXITCODE" }}
        Start-Process -FilePath (Join-Path $install '{escaped['exe']}') -WorkingDirectory $install
        throw 'updated client did not confirm startup; previous version restored'
    }}
}} catch {{
    if ((Test-Path -LiteralPath $backup) -and -not $updated) {{
        $null = & robocopy $backup $install /MIR /R:3 /W:1
        Start-Process -FilePath (Join-Path $install '{escaped['exe']}') -WorkingDirectory $install -ErrorAction SilentlyContinue
    }}
}} finally {{
    Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $package -Force -ErrorAction SilentlyContinue
    if ($modelPackage) {{ Remove-Item -LiteralPath $modelPackage -Force -ErrorAction SilentlyContinue }}
    Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $healthMarker -Force -ErrorAction SilentlyContinue
    Get-ChildItem -LiteralPath $updateRoot -File -ErrorAction SilentlyContinue |
        Where-Object {{ $_.Name -like 'EVE-Sentry-Monitor-*.zip' -or $_.Name -like '*.zip.part' -or $_.Name -like 'apply-*.ps1' }} |
        Remove-Item -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
    if ((Test-Path -LiteralPath $updateRoot) -and -not (Get-ChildItem -LiteralPath $updateRoot -Force -ErrorAction SilentlyContinue)) {{
        Remove-Item -LiteralPath $updateRoot -Force -ErrorAction SilentlyContinue
    }}
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
    ) -> None:
        super().__init__(parent)
        self.manifest_url = str(manifest_url or update_manifest_url()).strip()
        self.installed_version = str(installed_version or current_version()).strip()
        self.update_dir = Path(update_dir or default_update_dir())
        cleanup_update_artifacts(self.update_dir)
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
        if partial.exists() and partial.stat().st_size > component.size:
            partial.unlink(missing_ok=True)
        self._resume_offset = partial.stat().st_size if partial.exists() else 0
        try:
            file_handle = partial.open("ab")
        except OSError as exc:
            self.state_changed.emit(f"无法保存更新包：{exc}", "重试", True)
            return
        self._busy = True
        self._current_component = component
        self._download_kind = str(kind)
        self._download_path = partial
        self._download_file = file_handle
        self._download_hash = hashlib.sha256()
        if self._resume_offset:
            with partial.open("rb") as existing:
                for chunk in iter(lambda: existing.read(1024 * 1024), b""):
                    self._download_hash.update(chunk)
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

    def install_and_restart(self) -> None:
        """Launch the detached updater and ask the application to exit."""
        if self._launch_installer():
            self.restart_requested.emit()

    def install_on_exit(self) -> bool:
        """Launch a ready update after the application has chosen to exit."""
        return self._launch_installer()

    def _launch_installer(self) -> bool:
        package = self._ready_path
        if package is None or self._installer_launched:
            return False
        if not (sys.platform == "win32" and getattr(sys, "frozen", False)):
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(package.parent)))
            self.state_changed.emit("已打开更新包位置", "检查更新", True)
            return False
        install_dir = Path(sys.executable).resolve().parent
        script_path = self.update_dir / f"apply-{self._release.version}.ps1"
        script_path.write_text(
            build_update_script(
                package,
                install_dir,
                Path(sys.executable).name,
                os.getpid(),
                self._model_ready_path,
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
        self._installer_launched = True
        self.state_changed.emit("正在安装更新", "安装中", False)
        return True

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
            self.state_changed.emit("更新包已就绪，退出时自动安装", "立即安装", True)
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
