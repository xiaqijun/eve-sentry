"""Asynchronous portable-client update support."""

from __future__ import annotations

import hashlib
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

from PyQt6.QtCore import QObject, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from app.version import current_version, update_manifest_url


class UpdateError(RuntimeError):
    """Raised when release metadata or a downloaded package is invalid."""


@dataclass(frozen=True)
class ReleaseInfo:
    """Validated fields from one update manifest."""

    version: str
    url: str
    sha256: str
    size: int
    filename: str


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
    return ReleaseInfo(version, url, sha256, size, filename)


def default_update_dir() -> Path:
    """Return the per-user directory used for update downloads."""
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "EVE Sentry" / "updates"
    return Path.home() / ".eve-sentry" / "updates"


def cleanup_update_artifacts(
    update_dir: Path,
    temp_dir: Path | None = None,
    stale_after_seconds: float = 24 * 60 * 60,
) -> None:
    """Remove packages and staging files left by completed or interrupted updates."""
    update_root = Path(update_dir)
    for pattern in (
        "EVE-Sentry-Monitor-*.zip",
        "EVE-Sentry-Monitor-*.zip.part",
        "apply-*.ps1",
    ):
        for path in update_root.glob(pattern):
            try:
                if path.is_file() or path.is_symlink():
                    path.unlink()
            except OSError:
                continue
    try:
        update_root.rmdir()
    except OSError:
        pass

    stage_root = Path(temp_dir or tempfile.gettempdir())
    stale_before = time.time() - max(0.0, float(stale_after_seconds))
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
) -> str:
    """Build the PowerShell script that replaces files after client exit."""
    values = {
        "package": str(package_path),
        "install": str(install_dir),
        "exe": executable_name,
    }
    escaped = {key: value.replace("'", "''") for key, value in values.items()}
    return f"""$ErrorActionPreference = 'Stop'
$package = '{escaped['package']}'
$install = '{escaped['install']}'
$updateRoot = Split-Path -Parent $PSCommandPath
$stage = Join-Path ([IO.Path]::GetTempPath()) ('eve-sentry-update-' + [guid]::NewGuid())
try {{
    Wait-Process -Id {int(process_id)} -ErrorAction SilentlyContinue
    Expand-Archive -LiteralPath $package -DestinationPath $stage -Force
    $source = Join-Path $stage 'EVE-Sentry-Monitor-ONNX'
    if (-not (Test-Path -LiteralPath $source)) {{
        $directories = @(Get-ChildItem -LiteralPath $stage -Directory)
        if ($directories.Count -eq 1) {{ $source = $directories[0].FullName }} else {{ $source = $stage }}
    }}
    $null = & robocopy $source $install /MIR /R:3 /W:1
    if ($LASTEXITCODE -ge 8) {{ throw "robocopy failed with exit code $LASTEXITCODE" }}
    Start-Process -FilePath (Join-Path $install '{escaped['exe']}') -WorkingDirectory $install
}} finally {{
    Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $package -Force -ErrorAction SilentlyContinue
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
    ) -> None:
        super().__init__(parent)
        self.manifest_url = str(manifest_url or update_manifest_url()).strip()
        self.installed_version = str(installed_version or current_version()).strip()
        self.update_dir = Path(update_dir or default_update_dir())
        cleanup_update_artifacts(self.update_dir)
        self._network = QNetworkAccessManager(self)
        self._release: ReleaseInfo | None = None
        self._download_reply: QNetworkReply | None = None
        self._download_file = None
        self._download_hash = hashlib.sha256()
        self._download_path: Path | None = None
        self._ready_path: Path | None = None
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
        self.update_dir.mkdir(parents=True, exist_ok=True)
        target = self.update_dir / release.filename
        partial = target.with_suffix(target.suffix + ".part")
        try:
            file_handle = partial.open("wb")
        except OSError as exc:
            self.state_changed.emit(f"无法保存更新包：{exc}", "重试", True)
            return
        self._busy = True
        self._download_path = partial
        self._download_file = file_handle
        self._download_hash = hashlib.sha256()
        self.state_changed.emit("正在下载 0%", "下载中", False)
        reply = self._network.get(self._request(QUrl(release.url), timeout_ms=60000))
        self._download_reply = reply
        reply.readyRead.connect(self._read_download_data)
        reply.downloadProgress.connect(self._on_download_progress)
        reply.finished.connect(lambda: self._finish_download(reply, target))

    def install_and_restart(self) -> None:
        """Launch the detached updater and ask the application to exit."""
        package = self._ready_path
        if package is None:
            return
        if not (sys.platform == "win32" and getattr(sys, "frozen", False)):
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(package.parent)))
            self.state_changed.emit("已打开更新包位置", "检查更新", True)
            return
        install_dir = Path(sys.executable).resolve().parent
        script_path = self.update_dir / f"apply-{self._release.version}.ps1"
        script_path.write_text(
            build_update_script(
                package,
                install_dir,
                Path(sys.executable).name,
                os.getpid(),
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
        self.state_changed.emit("正在安装更新", "安装中", False)
        self.restart_requested.emit()

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
            release = parse_release_manifest(payload)
            if is_newer_version(release.version, self.installed_version):
                self._release = release
                self.state_changed.emit(
                    f"发现新版本 v{release.version}",
                    "下载更新",
                    True,
                )
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
        data = bytes(self._download_reply.readAll())
        if data:
            self._download_file.write(data)
            self._download_hash.update(data)

    def _on_download_progress(self, received: int, total: int) -> None:
        if total > 0:
            percent = max(0, min(100, round(received * 100 / total)))
            self.state_changed.emit(f"正在下载 {percent}%", "下载中", False)

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
        try:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                raise UpdateError(reply.errorString())
            if partial is None or release is None:
                raise UpdateError("更新下载状态丢失")
            actual_size = partial.stat().st_size
            if actual_size != release.size:
                raise UpdateError(
                    f"文件大小不匹配（{actual_size}/{release.size}）"
                )
            digest = self._download_hash.hexdigest()
            if digest != release.sha256:
                raise UpdateError("SHA256 校验失败")
            partial.replace(target)
            self._ready_path = target
            self.state_changed.emit("更新包已就绪", "安装并重启", True)
        except (OSError, UpdateError) as exc:
            if partial is not None:
                partial.unlink(missing_ok=True)
            self.state_changed.emit(f"下载失败：{exc}", "重试", True)
        finally:
            reply.deleteLater()
