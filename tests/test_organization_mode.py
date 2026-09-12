"""Independent organization rollout and shadow classification contracts."""

from app.esi.organization_relations import RelationSource, RelationView
from app.esi.relation_shadow import ShadowComparisons, ShadowContacts
from app.esi.session import ContactStanding
from app.intel.enrichment import _apply_contact_or_neutral_standing
from app.server.personnel_settings import DEFAULTS, validate_settings
from tests.test_personnel_settings import settings_service  # noqa: F401
from tests.test_personnel_archive import archive_factory  # noqa: F401


def test_shadow_compares_without_changing_legacy_result():
    source = RelationSource("corporation", 10, successful_at=100, entries={("corporation", 20): -5})
    view = RelationView("context", 10, None, True, (source,), (True,))
    counts = ShadowComparisons()
    contacts = ShadowContacts([ContactStanding(99, "character", 10)], view, counts)
    result = _apply_contact_or_neutral_standing({"character_id": 99, "corporation_id": 20}, contacts)
    assert result["contact_standing"] == 10
    assert counts.snapshot()["friendly_to_hostile"] == 1
    assert counts.snapshot()["different"] == 1


def test_legacy_four_fields_default_to_off():
    assert validate_settings({key: value for key, value in DEFAULTS.items() if key != "organization_mode"}) == DEFAULTS


def test_old_admin_save_preserves_new_organization_mode(settings_service):  # noqa: F811
    manager, store, _ = settings_service
    first = manager.update({"revision": "environment", "values": {
        **DEFAULTS, "mode": "on", "organization_mode": "shadow"}}, "admin")
    runtime = store._personnel_runtime
    generation = store._personnel_generation
    values = {key: value for key, value in first["values"].items() if key != "organization_mode"}
    values["background_max"] = 1
    second = manager.update({"revision": first["revision"], "values": values}, "admin")
    assert second["effective"]["organization_mode"] == "shadow"
    third = manager.update({"revision": second["revision"], "values": {
        **second["values"], "organization_mode": "on"}}, "admin")
    assert third["effective"]["organization_mode"] == "on"
    assert store._personnel_runtime is runtime
    assert store._personnel_generation == generation + 1
    assert not third["apply_required"]
