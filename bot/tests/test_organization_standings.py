"""New organization status cannot inherit stale personal standing after item merges."""

import pytest

from eve_risk.alerts import _detector_item_is_hostile


@pytest.mark.parametrize("value,expected", [(1, False), (0, True), (-5, True)])
def test_organization_sign(value, expected):
    assert _detector_item_is_hostile({"metadata": {
        "standing_source": "esi_organization", "contact_standing": value}}) is expected


@pytest.mark.parametrize("metadata", [
    {"standing_source": "esi_organization_pending", "contact_standing": -10},
    {"contact_standing": -10, "character_profiles": [{"standing_source": "esi_organization_pending"}]},
])
def test_pending_blocks_stale_flat_metadata(metadata):
    assert not _detector_item_is_hostile({"metadata": metadata})


def test_visual_count_is_not_cleared_by_pending_personnel():
    assert _detector_item_is_hostile({"metadata": {
        "standing_source": "esi_organization_pending", "hostile_count": 1}})
