"""Probe the live EVE window and run OCR against the member list region."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.engine.capturer import Capturer
from app.engine.ocr import OCREngine
from app.models.region_prefs import RegionPreferences


def _write_stdout(text: str = "") -> None:
    line = f"{text}\n"
    try:
        sys.stdout.write(line)
        sys.stdout.flush()
    except UnicodeEncodeError:
        buffer = getattr(sys.stdout, "buffer", None)
        if buffer is None:
            fallback = line.encode("ascii", errors="backslashreplace").decode("ascii")
            sys.stdout.write(fallback)
            sys.stdout.flush()
            return
        buffer.write(line.encode("utf-8", errors="replace"))
        buffer.flush()


def _write_json(payload: Any) -> None:
    _write_stdout(json.dumps(payload, ensure_ascii=False, indent=2))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyword", default="EVE -")
    parser.add_argument("--window", type=int, default=0, help="0-based window index")
    parser.add_argument("--frames", type=int, default=3, help="number of OCR samples")
    parser.add_argument(
        "--engine",
        choices=("paddle", "onnx"),
        default=os.environ.get("EVE_SENTRY_OCR_BACKEND", "paddle"),
        help="OCR backend to benchmark",
    )
    parser.add_argument(
        "--out",
        default="_live_ocr_probe",
        help="output prefix for image/json artifacts",
    )
    parser.add_argument(
        "--default-region",
        action="store_true",
        help="ignore saved region_prefs.json and use the built-in default region",
    )
    parser.add_argument(
        "--diagnose-windows",
        action="store_true",
        help="print EVE window candidates and rejection reasons before probing",
    )
    parser.add_argument(
        "--diagnose-only",
        action="store_true",
        help="only print EVE window diagnostics; do not initialize OCR, capture, or write artifacts",
    )
    return parser.parse_args(argv)


def _window_process_name(psutil_module: Any, pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        return str(psutil_module.Process(pid).name())
    except Exception as exc:
        return f"unavailable:{exc}"


def _candidate_reason(
    *,
    title_match: bool,
    process_match: bool,
    visible: bool,
    minimized: bool,
    width: int,
    height: int,
) -> str:
    if not title_match and not process_match:
        return "not_eve_candidate"
    if minimized:
        return "minimized"
    if width <= 0 or height <= 0:
        return "zero_sized_client"
    if not visible:
        return "not_visible"
    return "usable_candidate"


def collect_window_diagnostics(
    keyword: str,
    *,
    win32gui_module: Any | None = None,
    win32process_module: Any | None = None,
    psutil_module: Any | None = None,
) -> list[dict[str, Any]]:
    if win32gui_module is None or win32process_module is None or psutil_module is None:
        import psutil as real_psutil
        import win32gui as real_win32gui
        import win32process as real_win32process

        win32gui_module = win32gui_module or real_win32gui
        win32process_module = win32process_module or real_win32process
        psutil_module = psutil_module or real_psutil

    keyword_lower = keyword.lower()
    candidates: list[dict[str, Any]] = []

    def callback(hwnd: int, _param: object) -> bool:
        title = str(win32gui_module.GetWindowText(hwnd))
        pid = 0
        process_name = ""
        try:
            _thread_id, pid = win32process_module.GetWindowThreadProcessId(hwnd)
            process_name = _window_process_name(psutil_module, pid)
        except Exception as exc:
            process_name = f"unavailable:{exc}"

        title_match = title.lower().startswith(keyword_lower)
        process_match = "exefile" in process_name.lower()
        broad_eve_hint = "eve" in title.lower() or "eve" in process_name.lower()
        if not title_match and not process_match and not broad_eve_hint:
            return True

        visible = bool(win32gui_module.IsWindowVisible(hwnd))
        minimized = bool(win32gui_module.IsIconic(hwnd))
        try:
            rect = tuple(int(item) for item in win32gui_module.GetWindowRect(hwnd))
        except Exception:
            rect = (0, 0, 0, 0)
        try:
            client_rect = tuple(int(item) for item in win32gui_module.GetClientRect(hwnd))
        except Exception:
            client_rect = (0, 0, 0, 0)
        width = client_rect[2] - client_rect[0]
        height = client_rect[3] - client_rect[1]

        candidates.append(
            {
                "hwnd": hwnd,
                "pid": pid,
                "title": title,
                "process": process_name,
                "visible": visible,
                "minimized": minimized,
                "window_rect": list(rect),
                "client_rect": list(client_rect),
                "client_size": [width, height],
                "title_match": title_match,
                "process_match": process_match,
                "reason": _candidate_reason(
                    title_match=title_match,
                    process_match=process_match,
                    visible=visible,
                    minimized=minimized,
                    width=width,
                    height=height,
                ),
            }
        )
        return True

    win32gui_module.EnumWindows(callback, None)
    candidates.sort(
        key=lambda item: (
            item["reason"] != "usable_candidate",
            not item["process_match"],
            not item["title_match"],
            str(item["title"]),
        )
    )
    return candidates


def print_window_diagnostics(keyword: str, windows: list[dict[str, Any]]) -> None:
    diagnostics = collect_window_diagnostics(keyword)
    payload = {
        "keyword": keyword,
        "usable_window_count": len(windows),
        "candidate_count": len(diagnostics),
        "candidates": diagnostics,
    }
    _write_stdout("EVE window diagnostics:")
    _write_json(payload)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    capturer = Capturer()
    windows = capturer.list_eve_windows(args.keyword)
    if args.diagnose_windows or args.diagnose_only:
        print_window_diagnostics(args.keyword, windows)
    if args.diagnose_only:
        capturer.close()
        return 0 if windows else 1
    if not windows:
        _write_stdout("No usable EVE windows detected. Restore EVE if it is minimized.")
        if not args.diagnose_windows:
            print_window_diagnostics(args.keyword, windows)
        return 1
    if args.window < 0 or args.window >= len(windows):
        _write_stdout(f"Window index {args.window} is out of range (found {len(windows)}).")
        return 1

    window = windows[args.window]
    capturer.select_window(window["hwnd"], window["title"], window["w"], window["h"])
    region = None
    if not args.default_region:
        region = RegionPreferences("region_prefs.json").resolve_region(window)
    if region is None:
        region = capturer.get_member_list_region(window)

    _write_stdout("Selected window:")
    _write_json(window)
    _write_stdout("Probe region:")
    _write_json(region)

    engine = OCREngine(
        lang="en",
        confidence_threshold=0.7,
        backend=args.engine,
    )
    prefix = Path(args.out)
    all_results: list[dict] = []

    for index in range(args.frames):
        started = time.perf_counter()
        image = capturer.screenshot(region["x"], region["y"], region["w"], region["h"])
        capture_ms = (time.perf_counter() - started) * 1000

        if index == 0:
            image_path = prefix.with_name(f"{prefix.name}_capture.png")
            image.save(image_path)
            _write_stdout(f"Saved capture to {image_path}")

        ocr_started = time.perf_counter()
        lines = engine.recognize(image)
        ocr_ms = (time.perf_counter() - ocr_started) * 1000
        unique_lines = {text.strip() for text, _confidence in lines if text.strip()}

        frame_result = {
            "frame": index + 1,
            "capture_ms": round(capture_ms, 1),
            "ocr_ms": round(ocr_ms, 1),
            "line_count": len(lines),
            "unique_count": len(unique_lines),
            "lines": lines,
        }
        all_results.append(frame_result)

        _write_stdout(
            f"Frame {index + 1}: capture={capture_ms:.1f}ms "
            f"ocr={ocr_ms:.1f}ms lines={len(lines)} unique={len(unique_lines)}"
        )
        for text, confidence in lines[:15]:
            _write_stdout(f"  {confidence:.2f}  {text}")

    json_path = prefix.with_name(f"{prefix.name}_results.json")
    json_path.write_text(
        json.dumps(
            {
                "engine": args.engine,
                "window": window,
                "region": region,
                "frames": all_results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    _write_stdout(f"Saved OCR results to {json_path}")
    capturer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
