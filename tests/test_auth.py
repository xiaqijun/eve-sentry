from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from app.esi.sso import AuthorizationSession, EsiSsoError, TokenSet
from app.server.auth import AuthError, AuthService
from app.server.auth_store import AuthRepository
from app.server.sqlite_store import SQLiteIntelStore


class FakeResolver:
    def __init__(self) -> None:
        self.characters = {
            "Alice": {"character_id": 101, "corporation_id": 9001, "corporation_name": "Blue Corp"},
            "Mallory": {"character_id": 202, "corporation_id": 9002, "corporation_name": "Red Corp"},
        }

    def resolve_names(self, names):
        return [
            SimpleNamespace(name=name, category="character", entity_id=self.characters[name]["character_id"])
            for name in names
            if name in self.characters
        ]

    def character_profile(self, character_id):
        for name, value in self.characters.items():
            if value["character_id"] == int(character_id):
                return {"name": name, **value}
        raise RuntimeError("not found")

    def corporation_profile(self, corporation_id):
        return {"corporation_id": int(corporation_id), "name": "Blue Corp"}


class FakeSsoClient:
    def __init__(self, character_id=101, fail=False):
        self.character_id = character_id
        self.fail = fail

    def create_authorization_session(self, scopes=None):
        return AuthorizationSession(
            authorization_url="https://login.eve.test/authorize?state=state-1",
            state="state-1",
            redirect_uri="http://sentry.test/api/v1/auth/esi/callback",
            code_verifier="verifier",
            scopes=list(scopes or []),
        )

    def parse_callback_url(self, session, callback_url):
        query = parse_qs(urlparse(callback_url).query)
        if query.get("state", [""])[0] != session.state:
            raise EsiSsoError("state mismatch")
        return query.get("code", [""])[0]

    def exchange_code(self, code, session):
        if self.fail:
            raise EsiSsoError("token endpoint unavailable")
        return TokenSet(access_token="token", character_id=self.character_id)


@pytest.fixture()
def auth(tmp_path):
    store = SQLiteIntelStore(tmp_path / "intel.sqlite3")
    return AuthService(AuthRepository(store._connect), FakeResolver())


def _member(auth):
    return auth.create_user("pilot", "a-strong-password", role="member")


def test_login_session_and_api_key_secrets_are_not_returned_from_lists(auth):
    user = auth.create_user("admin", "a-strong-password", role="admin")
    login = auth.login("admin", "a-strong-password", "test")
    principal = auth.authenticate_session(login["session_token"])
    assert principal.user_id == user["user_id"]
    assert principal.csrf_token == login["csrf_token"]

    created = auth.create_api_key(user["user_id"], "Desktop", user["user_id"])
    assert created["secret"].startswith("eve_")
    assert "secret" not in auth.list_api_keys(user["user_id"])[0]
    principal = auth.authenticate_api_key(created["secret"])
    assert principal.identity_verified is False


def test_manually_revoked_api_key_can_be_enabled_then_deleted(auth):
    user = auth.create_user("admin", "a-strong-password", role="admin")
    login = auth.login("admin", "a-strong-password", "test")
    principal = auth.authenticate_session(login["session_token"])
    created = auth.create_api_key(user["user_id"], "Desktop", user["user_id"])

    auth.revoke_api_key(created["key_id"], principal)
    revoked = auth.repository.api_key_by_id(created["key_id"])
    assert revoked["status"] == "revoked"
    assert revoked["revoked_reason"] == "revoked by user"

    auth.enable_api_key(created["key_id"], principal)
    enabled = auth.repository.api_key_by_id(created["key_id"])
    assert enabled["status"] == "active"
    assert enabled["revoked_at"] == ""
    assert enabled["revoked_reason"] == ""

    auth.revoke_api_key(created["key_id"], principal)
    auth.delete_api_key(created["key_id"], principal)
    assert auth.repository.api_key_by_id(created["key_id"]) is None
    assert [item["action"] for item in auth.repository.list_audit()[:4]] == [
        "api_key.deleted",
        "api_key.revoked",
        "api_key.enabled",
        "api_key.revoked",
    ]


def test_automatically_revoked_api_key_cannot_be_enabled(auth):
    admin = auth.create_user("admin", "admin-password-123", role="admin")
    member = _member(auth)
    created = auth.create_api_key(member["user_id"], "Desktop", admin["user_id"])
    login = auth.login("admin", "admin-password-123", "test")
    principal = auth.authenticate_session(login["session_token"])
    auth.set_user_status(member["user_id"], False, admin["user_id"])
    auth.set_user_status(member["user_id"], True, admin["user_id"])

    with pytest.raises(AuthError) as exc_info:
        auth.enable_api_key(created["key_id"], principal)

    assert exc_info.value.code == "api_key_restore_forbidden"


def test_member_password_login_requires_eve_sso(auth):
    _member(auth)

    with pytest.raises(AuthError) as exc_info:
        auth.login("pilot", "a-strong-password", "test")

    assert exc_info.value.status == 403
    assert exc_info.value.code == "eve_sso_required"


def test_administrator_deletes_user_and_owned_authentication_records(auth):
    admin = auth.create_user("admin", "admin-password-123", role="admin")
    member = _member(auth)
    auth.create_api_key(member["user_id"], "Desktop", member["user_id"])
    auth.add_whitelist_character(member["user_id"], 101, "main", admin["user_id"])
    auth.repository.upsert_verified_character({
        "user_id": member["user_id"],
        "character_id": 101,
        "character_name": "Alice",
        "corporation_id": 9001,
        "corporation_name": "Blue Corp",
        "first_seen_at": "2026-07-28T00:00:00+00:00",
        "last_seen_at": "2026-07-28T00:00:00+00:00",
    })

    auth.delete_user(member["user_id"], admin["user_id"])

    assert auth.repository.user_by_id(member["user_id"]) is None
    assert auth.repository.list_api_keys(member["user_id"]) == []
    assert auth.repository.list_whitelist(member["user_id"]) == []
    assert auth.repository.list_verified_characters(member["user_id"]) == []
    assert auth.repository.list_audit()[0]["action"] == "user.deleted"


def test_administrator_cannot_delete_current_account_or_last_admin(auth):
    admin = auth.create_user("admin", "admin-password-123", role="admin")

    with pytest.raises(AuthError) as self_error:
        auth.delete_user(admin["user_id"], admin["user_id"])
    assert self_error.value.code == "cannot_delete_self"

    member = _member(auth)
    with pytest.raises(AuthError) as last_admin_error:
        auth.delete_user(admin["user_id"], member["user_id"])
    assert last_admin_error.value.code == "cannot_delete_last_admin"


def test_eve_sso_logs_in_exactly_assigned_active_member(auth):
    user = _member(auth)
    auth.add_allowed_corporation(9001, user["user_id"])
    auth.add_whitelist_character(user["user_id"], 101, "main", user["user_id"])
    auth.esi_sso_client = FakeSsoClient(101)

    assert auth.begin_esi_login("/account/keys").startswith("https://login.eve.test/")
    assert auth.owns_esi_login_callback(
        "/api/v1/auth/esi/callback?state=state-1&code=code-1"
    ) is True
    assert auth.owns_esi_login_callback(
        "/api/v1/auth/esi/callback?state=another-state&code=code-1"
    ) is False
    login = auth.complete_esi_login(
        "/api/v1/auth/esi/callback?state=state-1&code=code-1"
    )

    assert login["return_to"] == "/account/keys"
    assert login["user"]["user_id"] == user["user_id"]
    assert auth.authenticate_session(login["session_token"]).role == "member"


def test_eve_sso_rejects_unknown_ambiguous_and_replayed_characters(auth):
    first = _member(auth)
    auth.add_allowed_corporation(9001, first["user_id"])
    second = auth.create_user("pilot-two", "another-password", role="member")
    auth.add_whitelist_character(first["user_id"], 101, "main", first["user_id"])
    auth.add_whitelist_character(second["user_id"], 101, "alt", first["user_id"])
    auth.esi_sso_client = FakeSsoClient(101)
    auth.begin_esi_login()

    with pytest.raises(AuthError) as exc_info:
        auth.complete_esi_login("/callback?state=state-1&code=code-1")
    assert exc_info.value.code == "eve_character_ambiguous"

    with pytest.raises(AuthError) as replay_info:
        auth.complete_esi_login("/callback?state=state-1&code=code-1")
    assert replay_info.value.code == "invalid_esi_state"

    auth.esi_sso_client = FakeSsoClient(202)
    auth.begin_esi_login()
    with pytest.raises(AuthError) as unknown_info:
        auth.complete_esi_login("/callback?state=state-1&code=code-2")
    assert unknown_info.value.code == "eve_corporation_not_allowed"


def test_eve_sso_auto_creates_and_reuses_member_for_allowed_corporation(auth):
    auth.add_allowed_corporation(9001, "bootstrap")
    auth.esi_sso_client = FakeSsoClient(101)

    auth.begin_esi_login()
    first_login = auth.complete_esi_login("/callback?state=state-1&code=code-1")
    auth.begin_esi_login()
    second_login = auth.complete_esi_login("/callback?state=state-1&code=code-2")

    assert first_login["user"]["role"] == "member"
    assert first_login["user"]["display_name"] == "Alice"
    assert second_login["user"]["user_id"] == first_login["user"]["user_id"]
    assert len(auth.repository.list_users()) == 1
    assert auth.repository.list_verified_characters(first_login["user"]["user_id"])[0][
        "character_id"
    ] == 101


def test_eve_sso_rejects_assigned_member_outside_allowed_corporations(auth):
    user = _member(auth)
    auth.add_whitelist_character(user["user_id"], 202, "alt", user["user_id"])
    auth.esi_sso_client = FakeSsoClient(202)
    auth.begin_esi_login()

    with pytest.raises(AuthError) as exc_info:
        auth.complete_esi_login("/callback?state=state-1&code=code-1")

    assert exc_info.value.code == "eve_corporation_not_allowed"
    assert auth.repository.list_verified_characters(user["user_id"]) == []


def test_eve_sso_network_failure_does_not_create_session(auth):
    user = _member(auth)
    auth.add_whitelist_character(user["user_id"], 101, "main", user["user_id"])
    auth.esi_sso_client = FakeSsoClient(101, fail=True)
    auth.begin_esi_login()

    with pytest.raises(AuthError) as exc_info:
        auth.complete_esi_login("/callback?state=state-1&code=code-1")

    assert exc_info.value.code == "identity_validation_unavailable"
    assert auth.repository.list_audit()[-1]["action"] != "session.login"


def test_password_and_api_key_secrets_are_only_persisted_as_hashes(auth):
    user = _member(auth)
    stored_user = auth.repository.user_by_id(user["user_id"])
    assert stored_user is not None
    assert stored_user["password_hash"].startswith("$argon2id$")
    assert stored_user["password_hash"] != "a-strong-password"

    created = auth.create_api_key(user["user_id"], "Desktop", user["user_id"])
    stored_key = auth.repository.api_key_by_id(created["key_id"])
    assert stored_key is not None
    assert stored_key["key_hash"] != created["secret"]
    assert created["secret"] not in str(stored_key)


def test_login_rate_limit_blocks_repeated_failures(auth):
    _member(auth)
    for _ in range(5):
        with pytest.raises(AuthError) as exc_info:
            auth.login("pilot", "wrong-password", "198.51.100.10")
        assert exc_info.value.code == "invalid_credentials"

    with pytest.raises(AuthError) as exc_info:
        auth.login("pilot", "a-strong-password", "198.51.100.10")
    assert exc_info.value.status == 429
    assert exc_info.value.code == "login_rate_limited"


def test_allowed_corporation_permanently_verifies_desktop_key(auth):
    user = _member(auth)
    auth.add_allowed_corporation(9001, user["user_id"])
    created = auth.create_api_key(user["user_id"], "Desktop", user["user_id"])
    pending = auth.authenticate_api_key(created["secret"], allow_unverified=True)

    result = auth.verify_characters(pending, ["Alice"])

    assert result["verified"] is True
    assert result["permanent"] is True
    assert auth.authenticate_api_key(created["secret"]).identity_verified is True


def test_user_bound_character_whitelist_allows_character(auth):
    user = _member(auth)
    auth.add_whitelist_character(user["user_id"], 202, "alt", user["user_id"])
    created = auth.create_api_key(user["user_id"], "Desktop", user["user_id"])
    pending = auth.authenticate_api_key(created["secret"], allow_unverified=True)

    assert auth.verify_characters(pending, ["Mallory"])["verified"] is True


def test_confirmed_unauthorized_character_revokes_only_submitting_key(auth):
    user = _member(auth)
    first = auth.create_api_key(user["user_id"], "One", user["user_id"])
    second = auth.create_api_key(user["user_id"], "Two", user["user_id"])
    pending = auth.authenticate_api_key(first["secret"], allow_unverified=True)

    with pytest.raises(AuthError) as exc_info:
        auth.verify_characters(pending, ["Mallory"])

    assert exc_info.value.code == "unauthorized_eve_character"
    assert auth.repository.user_by_id(user["user_id"])["status"] == "active"
    assert auth.repository.api_key_by_id(first["key_id"])["status"] == "revoked"
    assert auth.repository.api_key_by_id(second["key_id"])["status"] == "active"
    assert auth.authenticate_api_key(second["secret"], allow_unverified=True).user_id == user["user_id"]
    audit = auth.repository.list_audit()[0]
    assert audit["action"] == "identity.key_revoked"
    assert audit["details"]["api_key_id"] == first["key_id"]


def test_unresolved_character_blocks_without_disabling_user(auth):
    user = _member(auth)
    created = auth.create_api_key(user["user_id"], "Desktop", user["user_id"])
    pending = auth.authenticate_api_key(created["secret"], allow_unverified=True)

    with pytest.raises(AuthError) as exc_info:
        auth.verify_characters(pending, ["Unknown Person"])

    assert exc_info.value.status == 503
    assert auth.repository.user_by_id(user["user_id"])["status"] == "active"


def test_missing_listener_does_not_disable_user_or_key(auth):
    user = _member(auth)
    created = auth.create_api_key(user["user_id"], "Desktop", user["user_id"])
    principal = auth.authenticate_api_key(created["secret"])

    with pytest.raises(AuthError) as exc_info:
        auth.verify_characters(principal, [])

    assert exc_info.value.code == "eve_listener_required"
    assert auth.repository.user_by_id(user["user_id"])["status"] == "active"
    assert auth.repository.api_key_by_id(created["key_id"])["status"] == "active"


def test_removed_rule_revokes_desktop_keys_without_disabling_user(auth):
    user = _member(auth)
    auth.add_allowed_corporation(9001, user["user_id"])
    created = auth.create_api_key(user["user_id"], "Desktop", user["user_id"])
    service = auth.create_api_key(
        user["user_id"], "Service", user["user_id"], key_type="service_readonly"
    )
    pending = auth.authenticate_api_key(created["secret"], allow_unverified=True)
    auth.verify_characters(pending, ["Alice"])

    auth.delete_allowed_corporation(9001, user["user_id"])
    assert auth.repository.user_by_id(user["user_id"])["status"] == "active"
    assert auth.repository.api_key_by_id(created["key_id"])["status"] == "revoked"
    assert auth.repository.api_key_by_id(service["key_id"])["status"] == "active"
    assert auth.authenticate_api_key(service["secret"]).is_read_only is True
    with pytest.raises(AuthError):
        auth.authenticate_api_key(created["secret"], allow_unverified=True)
