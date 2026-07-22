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
        "--fake-window-count",
        type=int,
        default=0,
        help="populate the window selector with this many fake EVE windows",
    )
    parser.add_argument(
        "--keep-cwd",
        action="store_true",
        help="do not switch to an isolated temporary runtime directory",
    )
    parser.add_argument(
        "--start-monitor",
        action="store_true",
        help="click Start Monitor after rendering",
    )
    parser.add_argument(
        "--screenshot",
        type=Path,
        help="write an offscreen PNG screenshot to this path",
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
        self.heartbeat_posts = 0
        self.ocr_snapshot_posts = 0
        self.tray_setup_patched = False

    def as_dict(self) -> dict[str, int | bool]:
        return dict(self.__dict__)


def build_fake_window(index: int = 0) -> dict:
    return {
        "hwnd": 1001 + index,
        "title": f"EVE - Smoke Pilot {index + 1}" if index else "EVE - Smoke Pilot",
        "x": 100 + (index * 32),
        "y": 80 + (index * 24),
        "w": 1280,
        "h": 720,
    }


def build_fake_windows(count: int) -> list[dict]:
    return [build_fake_window(index) for index in range(max(0, count))]


def widget_geometry(widget) -> dict[str, int]:
    rect = widget.geometry()
    return {
        "x": rect.x(),
        "y": rect.y(),
        "width": rect.width(),
        "height": rect.height(),
    }


def rects_overlap(left: dict[str, int], right: dict[str, int]) -> bool:
    return not (
        left["x"] + left["width"] <= right["x"]
        or right["x"] + right["width"] <= left["x"]
        or left["y"] + left["height"] <= right["y"]
        or right["y"] + right["height"] <= left["y"]
    )


def contains_all(text: str, needles: list[str]) -> bool:
    return all(needle in text for needle in needles)


def run_smoke(args: argparse.Namespace) -> dict:
    started_at = time.perf_counter()
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if args.start_monitor:
        os.environ["EVE_SENTRY_PUBLISH_INTEL"] = "1"
        os.environ["EVE_SENTRY_AUTO_START_MONITOR"] = "1"
    else:
        os.environ.setdefault("EVE_SENTRY_PUBLISH_INTEL", "0")
    os.environ.setdefault("EVE_SENTRY_USE_ESI_LOCATION", "0")
    os.environ.setdefault("EVE_SENTRY_SYSTEM", "Smoke")
    os.environ.setdefault("EVE_SENTRY_OCR_DEVICE", "cpu")
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "modelscope")
    fake_window_count = max(1 if args.fake_window else 0, args.fake_window_count)
    fake_windows = build_fake_windows(fake_window_count)
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
                return [dict(window) for window in fake_windows]

            def get_window_info(self, hwnd: int) -> dict | None:
                counters.get_window_info_calls += 1
                return next(
                    (dict(window) for window in fake_windows if hwnd == window["hwnd"]),
                    None,
                )

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

            def get_member_list_region(self, window: dict) -> dict:
                return {
                    "x": int(window["x"]) + int(window["w"]) - 220,
                    "y": int(window["y"]) + 80,
                    "w": 200,
                    "h": int(window["h"]) - 120,
                }

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

        class SmokeIntelClient:
            def __init__(self, *args, **kwargs) -> None:
                _ = args, kwargs
                counters.intel_client_created += 1
                self.heartbeats: list[dict] = []

            def post_heartbeat(self, **payload):
                counters.heartbeat_posts += 1
                self.heartbeats.append(payload)
                return {"client_id": payload.get("client_id", ""), "online": True}

            def post_ocr_snapshot(self, **payload):
                _ = payload
                counters.ocr_snapshot_posts += 1
                return {"created": 0, "active": [], "inactive": []}

        def fake_setup_tray(self) -> None:
            counters.tray_setup_patched = True
            self._tray = None

        main_window.Capturer = FakeCapturer
        main_window.OCREngine = FakeOCREngine
        main_window.IntelApiClient = SmokeIntelClient if args.start_monitor else ForbiddenIntelClient
        main_window.MainWindow._setup_tray = fake_setup_tray

        app = QApplication.instance() or QApplication(["monitor-ui-smoke"])
        window = main_window.MainWindow()
        window.resize(window.minimumWidth(), window.minimumHeight())
        window.show()
        app.processEvents()

        if args.start_monitor:
            app.processEvents()

        status_cards = getattr(window, "_status_cards", {})
        status_card_values = {
            key: value_label.text()
            for key, (_frame, _title, value_label) in status_cards.items()
        }
        status_card_details = {
            key: {
                "object_name": frame.objectName(),
                "minimum_height": frame.minimumHeight(),
                "geometry": widget_geometry(frame),
                "title": title_label.text(),
                "value": value_label.text(),
                "has_qframe_style": "QFrame" in frame.styleSheet(),
                "has_transparent_label_style": (
                    "background: transparent" in frame.styleSheet()
                ),
            }
            for key, (frame, title_label, value_label) in status_cards.items()
        }
        layout_rects = {
            "window": {
                "x": window.x(),
                "y": window.y(),
                "width": window.width(),
                "height": window.height(),
            },
            "settings": widget_geometry(window._settings),
            "monitor_button": widget_geometry(window._monitor_btn),
            "window_combo": widget_geometry(window._window_combo),
            "window_label": widget_geometry(window._window_label),
            "window_status_table": widget_geometry(window._window_status_table),
            "log": widget_geometry(window._log),
        }
        right_controls = [
            layout_rects["monitor_button"],
            layout_rects["window_combo"],
            layout_rects["window_label"],
            layout_rects["window_status_table"],
            layout_rects["log"],
        ]
        layout_checks = {
            "settings_width": layout_rects["settings"]["width"],
            "settings_has_expected_width": layout_rects["settings"]["width"] == 240,
            "monitor_button_height": layout_rects["monitor_button"]["height"],
            "monitor_button_has_readable_height": (
                layout_rects["monitor_button"]["height"] >= 36
            ),
            "right_controls_do_not_overlap": all(
                not rects_overlap(left, right)
                for index, left in enumerate(right_controls)
                for right in right_controls[index + 1 :]
            ),
        }
        pixmap = window.grab()
        screenshot_path = args.screenshot
        if screenshot_path is None:
            screenshot_path = Path.cwd() / "monitor-ui-smoke.png"
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        screenshot_saved = pixmap.save(str(screenshot_path), "PNG")
        screenshot_size = screenshot_path.stat().st_size if screenshot_saved else 0
        window_combo_items = [
            window._window_combo.itemText(index)
            for index in range(window._window_combo.count())
        ]
        status_table_rows = []
        for row in range(window._window_status_table.rowCount()):
            status_table_rows.append(
                [
                    (
                        window._window_status_table.item(row, column).text()
                        if window._window_status_table.item(row, column) is not None
                        else ""
                    )
                    for column in range(window._window_status_table.columnCount())
                ]
            )
        inactive_button_style = main_window.monitor_button_style(active=False)
        active_button_style = main_window.monitor_button_style(active=True)
        monitor_button_stylesheet = window._monitor_btn.styleSheet()
        theme_checks = {
            "app_qss_has_shell_colors": contains_all(
                window.styleSheet(),
                ["#061017", "#040c12", "QStatusBar", "QPushButton"],
            ),
            "inactive_button_style_applied": (
                monitor_button_stylesheet == inactive_button_style
            ),
            "inactive_button_style_has_accent": contains_all(
                inactive_button_style,
                ["#0d5f75", "#23b7d8", "font-size: 16px"],
            ),
            "active_button_style_has_danger": contains_all(
                active_button_style,
                ["#b52b28", "#ff5b50", "font-size: 16px"],
            ),
            "all_status_cards_named": all(
                detail["object_name"] == f"status-card-{key}"
                for key, detail in status_card_details.items()
            ),
            "all_status_cards_have_min_height": all(
                detail["minimum_height"] >= 58
                for detail in status_card_details.values()
            ),
            "all_status_cards_have_frame_style": all(
                detail["has_qframe_style"]
                for detail in status_card_details.values()
            ),
            "all_status_cards_have_transparent_label_style": all(
                detail["has_transparent_label_style"]
                for detail in status_card_details.values()
            ),
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
            "monitoring": window._is_monitoring(),
            "worker_count": worker_count,
            "window_combo_count": window._window_combo.count(),
            "window_combo_items": window_combo_items,
            "selected_window": window._window_combo.currentText(),
            "window_label": window._window_label.text(),
            "window_status_rows": status_table_rows,
            "monitor_button": window._monitor_btn.text(),
            "status_card_keys": list(status_cards.keys()),
            "status_card_values": status_card_values,
            "style_applied": "QMainWindow" in window.styleSheet(),
            "theme_checks": theme_checks,
            "monitor_button_style": {
                "inactive_applied": theme_checks["inactive_button_style_applied"],
                "inactive_contains": ["#0d5f75", "#23b7d8", "font-size: 16px"],
                "active_contains": ["#b52b28", "#ff5b50", "font-size: 16px"],
            },
            "status_card_details": status_card_details,
            "layout": layout_rects,
            "layout_checks": layout_checks,
            "screenshot": {
                "captured": bool(screenshot_saved),
                "path": str(screenshot_path),
                "width": pixmap.width(),
                "height": pixmap.height(),
                "bytes": screenshot_size,
            },
            "runtime_files_created": sorted(
                path.name
                for path in Path.cwd().glob("*")
                if path.is_file() and path != screenshot_path
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
