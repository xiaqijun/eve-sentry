from eve_risk.domain import ShipRole
from eve_risk.ship_roles import ShipRoleClassifier


def test_role_classifier_uses_groups_and_combat_fallback() -> None:
    classifier = ShipRoleClassifier()
    assert classifier.classify(1, "Logistics", 6) == ShipRole.LOGISTICS
    assert classifier.classify(2, "Heavy Interdictor", 6) == ShipRole.INTERDICTION
    assert classifier.classify(3, "Heavy Assault Cruiser", 6) == ShipRole.DPS
    assert classifier.classify(4, "Freighter", 6) == ShipRole.INDUSTRIAL
    assert classifier.classify(5, "Industrial Command Ship", 6) == ShipRole.INDUSTRIAL
