"""Bounded failure metadata, without exception messages, URLs or credentials."""

import re

from app.esi.contact_http import ContactReadError

CONTACT_REASONS = frozenset(
    {
        "invalid_contacts_payload",
        "invalid_contact_row",
        "duplicate_contact_across_pages",
        "contacts_deadline",
        "contacts_http_error",
        "contacts_304_without_body",
        "contacts_body_limit",
        "contacts_snapshot_limit",
        "contacts_pages_changed",
        "contacts_row_limit",
        "contacts_pages_expired",
        "contacts_read_failed",
        "invalid_own_organization",
        "organization_contacts_expiry_unavailable",
        "organization_authority_changed",
        "contacts_response_already_expired",
        "organization_contacts_unavailable",
        "contacts_group_expired_during_refresh",
        "own_profile_unavailable",
        "own_profile_invalid",
        "own_alliance_invalid",
        "contact_source_unavailable",
    }
)


def failure_summary(error: BaseException) -> str:
    """Expose safe codes through at most five wrapper exceptions."""
    fields, seen = [], set()
    for _ in range(5):
        if not isinstance(error, BaseException) or id(error) in seen:
            break
        seen.add(id(error))
        name = re.sub(r"[^A-Za-z0-9_]", "_", type(error).__name__)[:64]
        fields.append(f"{'error' if not fields else 'cause'}={name}")
        reason = error.args[0] if error.args else None
        if (
            isinstance(error, ContactReadError)
            and isinstance(reason, str)
            and reason in CONTACT_REASONS
        ):
            fields.append(f"reason={reason}")
        status = getattr(error, "status", None) or getattr(error, "code", None)
        if type(status) is int and 100 <= status <= 599:
            fields.append(f"status={status}")
        sqlstate = getattr(error, "sqlstate", None)
        if isinstance(sqlstate, str) and re.fullmatch(r"[A-Z0-9]{5}", sqlstate):
            fields.append(f"sqlstate={sqlstate}")
        error = (
            error.__cause__
            or (None if error.__suppress_context__ else error.__context__)
            or getattr(error, "reason", None)
        )
    return " ".join(fields)
