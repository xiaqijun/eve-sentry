"""ESI public-data and SSO integration helpers."""

_EXPORTS = {
    "ContactStanding": ("app.esi.session", "ContactStanding"),
    "EsiStanding": ("app.esi.session", "EsiStanding"),
    "DEFAULT_SCOPES": ("app.esi.sso", "DEFAULT_SCOPES"),
    "EsiAuthenticatedSession": ("app.esi.session", "EsiAuthenticatedSession"),
    "EsiSessionSnapshot": ("app.esi.session", "EsiSessionSnapshot"),
    "EsiRequestMetrics": ("app.esi.remote", "EsiRequestMetrics"),
    "RemoteEsiClient": ("app.esi.remote", "RemoteEsiClient"),
    "EsiSsoError": ("app.esi.sso", "EsiSsoError"),
    "EsiLoginManager": ("app.esi.sso", "EsiLoginManager"),
    "EsiTokenStore": ("app.esi.sso", "EsiTokenStore"),
    "EveSsoClient": ("app.esi.sso", "EveSsoClient"),
    "LocalCallbackServer": ("app.esi.sso", "LocalCallbackServer"),
    "TokenProtector": ("app.esi.sso", "TokenProtector"),
    "TokenSet": ("app.esi.sso", "TokenSet"),
    "WindowsDpapiTokenProtector": ("app.esi.sso", "WindowsDpapiTokenProtector"),
    "apply_contact_standing": ("app.esi.session", "apply_contact_standing"),
    "build_token_store": ("app.esi.sso", "build_token_store"),
    "contact_standings_from_payload": (
        "app.esi.session",
        "contact_standings_from_payload",
    ),
    "default_token_protector": ("app.esi.sso", "default_token_protector"),
    "matching_contact_standing": ("app.esi.session", "matching_contact_standing"),
    "standings_from_payload": ("app.esi.session", "standings_from_payload"),
    "run_local_sso_login": ("app.esi.sso", "run_local_sso_login"),
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
