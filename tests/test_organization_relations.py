"""The approved organizational precedence, stale protection and consumer behavior."""

from dataclasses import replace
from types import MappingProxyType

import pytest

from app.core.models import Observation
from app.esi.organization_relations import (
    PENDING_SOURCE,
    RelationSource,
    RelationView,
    organization_entries,
)
from app.intel.classification import ClassificationEngine
from app.intel.enrichment import _apply_contact_or_neutral_standing
from app.server.intel_store import IntelStore


def contact(kind, entity_id, standing):
    return {"contact_type": kind, "contact_id": entity_id, "standing": standing}


def view(corp=(), alliance=(), *, usable=(True, True), own_alliance=20):
    sources = (RelationSource("corporation", 10, 1, 1000, 1300, entries=organization_entries(list(corp))),
               RelationSource("alliance", 20, 1, 1000, 1300, entries=organization_entries(list(alliance))))
    if own_alliance is None:
        sources, usable = sources[:1], usable[:1]
    return RelationView("context", 10, own_alliance, True, sources, usable)


@pytest.mark.parametrize("personal", [-10, 0, 10])
def test_individual_relations_are_never_indexed(personal):
    result = view([contact("character", 99, personal)], [contact("character", 99, personal)])
    assert all(not source.entries for source in result.sources)
    assert result.annotate({"character_id": 99, "corporation_id": 30})["contact_standing"] == 0


@pytest.mark.parametrize("corp_value, alliance_value, expected", [(0, 5, 0), (5, -10, 5), (-5, 10, -5)])
def test_own_corporation_source_wins_for_same_target(corp_value, alliance_value, expected):
    result = view([contact("corporation", 30, corp_value)], [contact("corporation", 30, alliance_value)])
    assert result.annotate({"corporation_id": 30})["contact_standing"] == expected


def test_target_specificity_precedes_source_precedence():
    result = view([contact("alliance", 40, 10)], [contact("corporation", 30, 0)])
    annotated = result.annotate({"corporation_id": 30, "alliance_id": 40})
    assert annotated["contact_standing"] == 0
    assert annotated["standing_contact_type"] == "corporation"


@pytest.mark.parametrize("profile", [{"corporation_id": 10}, {"corporation_id": 30, "alliance_id": 20}])
def test_same_organization_wins_over_all_explicit_relations(profile):
    result = view([contact("corporation", 10, -10), contact("corporation", 30, -10)], usable=(False, False))
    assert result.annotate(profile)["contact_standing"] == 10


def test_no_alliance_is_not_a_shared_alliance():
    assert view(own_alliance=None).annotate({"corporation_id": 30})["contact_standing"] == 0


@pytest.mark.parametrize("profile", [{}, {"corporation_id": True}, {"corporation_id": 10, "affiliation_trusted": False}])
def test_unknown_affiliation_is_not_neutral_or_friendly(profile):
    result = view().annotate({**profile, "contact_standing": 10, "standing": -10, "standing_contact_type": "character"})
    assert result["standing_source"] == PENDING_SOURCE
    assert "contact_standing" not in result and "standing" not in result


def test_missing_higher_source_cannot_fall_through():
    result = view([], [contact("corporation", 30, 10)], usable=(False, True))
    assert result.annotate({"corporation_id": 30})["standing_source"] == PENDING_SOURCE
    result = view([contact("alliance", 40, 10)], [], usable=(True, False))
    assert result.annotate({"corporation_id": 30, "alliance_id": 40})["standing_source"] == PENDING_SOURCE
    result = view([contact("corporation", 30, 5)], [], usable=(True, False))
    assert result.annotate({"corporation_id": 30})["contact_standing"] == 5


def test_known_empty_source_defaults_neutral_without_building_a_dict_each_read():
    result = view()
    for _ in range(1000):
        profile = _apply_contact_or_neutral_standing({"corporation_id": 30}, result)
        assert profile["contact_standing"] == 0
    assert isinstance(result.sources[0].entries, MappingProxyType)


@pytest.mark.parametrize("value,expected", [(1, "white"), (.00001, "white"), (-.000000001, "red"), (0, "red"), (-5, "red")])
def test_report_and_current_roster_use_the_same_sign_rule(value, expected):
    profile = view([contact("corporation", 30, value)]).annotate({"character_id": 99, "corporation_id": 30})
    engine = ClassificationEngine()
    observation = Observation(source="local_ocr", system_name="Tama", names=["Pilot"],
                              metadata={"hostile_icon_count": 1})
    assert engine.classify(observation, ["Pilot"], [profile]).classification == expected
    store = IntelStore(systems={}, links=[], scorer=engine)
    try:
        item = {"source": "local_ocr", "name": "Pilot", "metadata": {
            "character_profiles": [profile], "contact_standing": -10}}
        assert store._active_item_is_hostile(item) is (expected == "red")
    finally:
        store.close()
    assert profile["contact_standing"] == value  # Do not rewrite the official value to ±10.


def test_pending_does_not_inherit_flat_old_personal_standing():
    profile = replace(view(), own_usable=False).annotate({"character_id": 99, "corporation_id": 30})
    store = IntelStore(systems={}, links=[], scorer=ClassificationEngine())
    try:
        assert not store._active_item_is_hostile({"source": "local_ocr", "metadata": {
            "character_profiles": [profile], "contact_standing": -10}})
    finally:
        store.close()
