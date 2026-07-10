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
        ("Hajimi6", 0.95),
    ]

    assert ocr_candidate_names(results) == ["Hajimi6"]
