"""Probe the live EVE window and run OCR against the member list region."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.engine.capturer import Capturer
from app.engine.ocr import OCREngine
from app.models.region_prefs import RegionPreferences


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyword", default="EVE -")
    parser.add_argument("--window", type=int, default=0, help="0-based window index")
    parser.add_argument("--frames", type=int, default=3, help="number of OCR samples")
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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    capturer = Capturer()
    windows = capturer.list_eve_windows(args.keyword)
    if not windows:
        print("No usable EVE windows detected. Restore EVE if it is minimized.")
        return 1
    if args.window < 0 or args.window >= len(windows):
        print(f"Window index {args.window} is out of range (found {len(windows)}).")
        return 1

    window = windows[args.window]
    capturer.select_window(window["hwnd"], window["title"], window["w"], window["h"])
    region = None
    if not args.default_region:
        region = RegionPreferences("region_prefs.json").resolve_region(window)
    if region is None:
        region = capturer.get_member_list_region(window)

    print("Selected window:")
    print(json.dumps(window, ensure_ascii=False, indent=2))
    print("Probe region:")
    print(json.dumps(region, ensure_ascii=False, indent=2))

    engine = OCREngine(lang="en", confidence_threshold=0.7)
    prefix = Path(args.out)
    all_results: list[dict] = []

    for index in range(args.frames):
        started = time.perf_counter()
        image = capturer.screenshot(region["x"], region["y"], region["w"], region["h"])
        capture_ms = (time.perf_counter() - started) * 1000

        if index == 0:
            image_path = prefix.with_name(f"{prefix.name}_capture.png")
            image.save(image_path)
            print(f"Saved capture to {image_path}")

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

        print(
            f"Frame {index + 1}: capture={capture_ms:.1f}ms "
            f"ocr={ocr_ms:.1f}ms lines={len(lines)} unique={len(unique_lines)}"
        )
        for text, confidence in lines[:15]:
            print(f"  {confidence:.2f}  {text}")

    json_path = prefix.with_name(f"{prefix.name}_results.json")
    json_path.write_text(
        json.dumps(
            {
                "window": window,
                "region": region,
                "frames": all_results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Saved OCR results to {json_path}")
    capturer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
