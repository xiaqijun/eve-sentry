"""Public ESI and personnel classification helpers."""

_EXPORTS = {
    "ContactStanding": ("app.esi.contact_models", "ContactStanding"),
    "EsiRequestMetrics": ("app.esi.remote", "EsiRequestMetrics"),
    "RemoteEsiClient": ("app.esi.remote", "RemoteEsiClient"),
    "apply_contact_standing": ("app.esi.contact_models", "apply_contact_standing"),
    "contact_standings_from_payload": (
        "app.esi.contact_models",
        "contact_standings_from_payload",
    ),
    "matching_contact_standing": (
        "app.esi.contact_models",
        "matching_contact_standing",
    ),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str):
    """Load ESI helper exports on demand."""
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc

    from importlib import import_module

    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value
