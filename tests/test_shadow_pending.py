"""Unknown classifications never count as verified friend/enemy differences."""

import pytest

from app.esi.relation_shadow import ShadowComparisons


@pytest.mark.parametrize(
    "before,after,pending,different",
    [
        (None, None, 1, 0),
        (10, None, 1, 0),
        (0, None, 1, 0),
        (None, -10, 1, 0),
        (10, 5, 0, 0),
        (-10, 0, 0, 0),
        (10, 0, 0, 1),
        (-5, 10, 0, 1),
    ],
)
def test_pending_and_real_differences_are_separate(before, after, pending, different):
    counter = ShadowComparisons()
    counter.compare({"contact_standing": before}, {"contact_standing": after})
    counts = counter.snapshot()
    assert counts["compared"] == 1
    assert counts["pending"] == pending
    assert counts["comparable"] == 1 - pending
    assert counts["decision_different"] == different


def test_repeated_pending_retains_legacy_counter_but_has_no_valid_samples():
    counter = ShadowComparisons()
    for _ in range(2288):
        counter.compare({"contact_standing": 10}, {})
    counts = counter.snapshot()
    assert counts["compared"] == counts["different"] == counts["pending"] == 2288
    assert counts["comparable"] == counts["decision_different"] == 0
