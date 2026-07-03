from app.engine.worker import build_ocr_snapshot_names, build_scan_status


def test_scan_status_counts_cleaned_member_names_not_raw_ocr_blocks():
    ocr_results = [
        ("+", 0.95),
        ("+ Alice", 0.95),
        ("Bob", 0.95),
    ]

    assert build_scan_status(ocr_results, ["Bob"]) == (
        "识别: 2 个成员 / 2 个唯一 / 1 个新告警"
    )


def test_ocr_snapshot_names_are_cleaned_member_names():
    ocr_results = [
        ("+", 0.95),
        ("+ Alice", 0.95),
        ("alice", 0.90),
        ("Bob, + Carol", 0.88),
    ]

    assert build_ocr_snapshot_names(ocr_results) == ["Alice", "Bob", "Carol"]
