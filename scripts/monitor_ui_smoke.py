"""Offscreen smoke check for the detector client main window."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the monitor client UI smoke check without real EVE or network access."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit a single JSON payload (default behavior)",
    )
    parser.add_argument(
        "--fake-window",
        action="store_true",
        help="populate the window selector with one fake EVE window",
    )
    parser.add_argument(
        "--keep-cwd",
        action="store_true",
        help="do not switch to an isolated temporary runtime directory",
    )
    return parser.parse_args(argv)


class SmokeCounters:
    def __init__(self) -> None:
        self.capturer_created = 0
        self.list_eve_windows_calls = 0
        self.get_window_info_calls = 0
        self.select_window_calls = 0
        self.activate_window_calls = 0
        self.screenshot_calls = 0
        self.capturer_close_calls = 0
        self.ocr_created = 0
        self.ocr_recognize_calls = 0
        self.intel_client_created = 0
        self.network_requests = 0
        self.tray_setup_patched = False

    def as_dict(self) -> dict[str, int | bool]:
        return dict(self.__dict__)


def build_fake_window() -> dict:
    return {
        "hwnd": 1001,
        "title": "EVE - Smoke Pilot",
        "x": 100,
        "y": 80,
        "w": 1280,
        "h": 720,
    }


def run_smoke(args: argparse.Namespace) -> dict:
    started_at = time.perf_counter()
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("EVE_SENTRY_PUBLISH_INTEL", "0")
    os.environ.setdefault("EVE_SENTRY_USE_ESI_LOCATION", "0")
    os.environ.setdefault("EVE_SENTRY_SYSTEM", "Smoke")
    os.environ.setdefault("EVE_SENTRY_OCR_DEVICE", "cpu")
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "modelscope")

    counters = SmokeCounters()
    original_cwd = Path.cwd()
    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    if not args.keep_cwd:
        temp_dir = tempfile.TemporaryDirectory(prefix="eve-sentry-ui-smoke-")
        os.chdir(temp_dir.name)

    try:
        from PyQt6.QtWidgets import QApplication

        import app.ui.main_window as main_window

        class FakeCapturer:
            def __init__(self) -> None:
                counters.capturer_created += 1

            def list_eve_windows(self, keyword: str = "EVE -") -> list[dict]:
                _ = keyword
                counters.list_eve_windows_calls += 1
                return [build_fake_window()] if args.fake_window else []

            def get_window_info(self, hwnd: int) -> dict | None:
                counters.get_window_info_calls += 1
                window = build_fake_window()
                return window if hwnd == window["hwnd"] else None

            def select_window(
                self,
                hwnd: int,
                title: str,
                w: int,
                h: int,
                start_capture: bool = True,
            ) -> None:
                _ = hwnd, title, w, h, start_capture
                counters.select_window_calls += 1

            def activate_window(self, hwnd: int) -> bool:
                _ = hwnd
                counters.activate_window_calls += 1
                return True

            def screenshot(self, region=None):
                _ = region
                counters.screenshot_calls += 1
                return None

            def close(self) -> None:
                counters.capturer_close_calls += 1

        class FakeOCREngine:
            def __init__(self, **kwargs) -> None:
                _ = kwargs
                counters.ocr_created += 1

            def recognize(self, image) -> list[str]:
                _ = image
                counters.ocr_recognize_calls += 1
                return []

        class ForbiddenIntelClient:
            def __init__(self, *args, **kwargs) -> None:
                _ = args, kwargs
                counters.intel_client_created += 1

            def __getattr__(self, name: str):
                def _forbidden(*args, **kwargs):
                    _ = name, args, kwargs
                    counters.network_requests += 1
                    raise AssertionError("network access is forbidden in UI smoke")

                return _forbidden

        def fake_setup_tray(self) -> None:
            counters.tray_setup_patched = True
            self._tray = None

        main_window.Capturer = FakeCapturer
        main_window.OCREngine = FakeOCREngine
        main_window.IntelApiClient = ForbiddenIntelClient
        main_window.MainWindow._setup_tray = fake_setup_tray

        app = QApplication.instance() or QApplication(["monitor-ui-smoke"])
        window = main_window.MainWindow()
        window.show()
        app.processEvents()

        status_cards = getattr(window, "_status_cards", {})
        status_card_values = {
            key: value_label.text()
            for key, (_frame, _title, value_label) in status_cards.items()
        }
        worker_count = len(getattr(window, "_workers", {}))
        payload = {
            "ok": True,
            "error": "",
            "qt_platform": os.environ.get("QT_QPA_PLATFORM", ""),
            "window_title": window.windowTitle(),
            "minimum_size": [window.minimumWidth(), window.minimumHeight()],
            "main_window_created": True,
            "intel_client_created": getattr(window, "_intel_client", None) is not None,
            "heartbeat_timer_active": window._heartbeat_timer.isActive(),
            "channel_timer_active": window._channel_timer.isActive(),
            "monitoring": window._is_monitoring(),
            "worker_count": worker_count,
            "window_combo_count": window._window_combo.count(),
            "window_label": window._window_label.text(),
            "monitor_button": window._monitor_btn.text(),
            "status_card_keys": list(status_cards.keys()),
            "status_card_values": status_card_values,
            "style_applied": "QMainWindow" in window.styleSheet(),
            "runtime_files_created": sorted(
                path.name for path in Path.cwd().glob("*") if path.is_file()
            ),
            "side_effects": counters.as_dict(),
            "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 2),
        }
        window._capturer.close()
        window.deleteLater()
        app.processEvents()
        return payload
    finally:
        if temp_dir is not None:
            os.chdir(original_cwd)
            temp_dir.cleanup()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = run_smoke(args)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0 if payload.get("ok") else 1
    except Exception as exc:  # pragma: no cover - exercised by subprocess callers.
        payload = {
            "ok": False,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
