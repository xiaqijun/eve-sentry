from app.engine.ocr_names import ocr_candidate_names


def test_ocr_candidate_names_remove_member_list_icons():
    results = [
        ("+", 0.95),
        ("+ Alice", 0.95),
        (" Bob", 0.95),
    ]

    assert ocr_candidate_names(results) == ["Alice", "Bob"]


def test_ocr_candidate_names_split_comma_rows_and_deduplicate_case():
    results = [
        ("Alice, + Bob", 0.95),
        ("alice", 0.90),
        ("+ Carol", 0.88),
    ]

    assert ocr_candidate_names(results) == ["Alice", "Bob", "Carol"]


def test_ocr_candidate_names_ignore_member_count_numbers():
    results = [
        ("3", 1.0),
        ("二8", 0.98),
        ("二 6", 0.97),
        ("Hajimi6", 0.95),
    ]

    assert ocr_candidate_names(results) == ["Hajimi6"]


def test_ocr_candidate_names_merges_zero_padded_numeric_suffix():
    results = [
        ("STARKEY", 0.95),
        ("07", 0.95),
        ("3", 1.0),
    ]

    assert ocr_candidate_names(results) == ["STARKEY 07"]


def test_ocr_candidate_names_ignore_distance_and_location_marker_noise():
    results = [
        ("527 m", 0.99),
        ("127 m", 0.99),
        ("95 km", 0.99),
        ("0.8 AU", 0.99),
        ("R-YWID*", 0.99),
        ("Enemy Pilot", 0.99),
    ]

    assert ocr_candidate_names(results) == ["Enemy Pilot"]
