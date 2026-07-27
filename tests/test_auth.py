from types import SimpleNamespace

import pytest

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


@pytest.fixture()
def auth(tmp_path):
    store = SQLiteIntelStore(tmp_path / "intel.sqlite3")
    return AuthService(AuthRepository(store._connect), FakeResolver())


def _member(auth):
    return auth.create_user("pilot", "a-strong-password", role="member")


def test_login_session_and_api_key_secrets_are_not_returned_from_lists(auth):
    user = _member(auth)
    login = auth.login("pilot", "a-strong-password", "test")
    principal = auth.authenticate_session(login["session_token"])
    assert principal.user_id == user["user_id"]
    assert principal.csrf_token == login["csrf_token"]

    created = auth.create_api_key(user["user_id"], "Desktop", user["user_id"])
    assert created["secret"].startswith("eve_")
    assert "secret" not in auth.list_api_keys(user["user_id"])[0]
    with pytest.raises(AuthError) as exc_info:
        auth.authenticate_api_key(created["secret"])
    assert exc_info.value.code == "identity_validation_required"


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


def test_confirmed_unauthorized_character_disables_user_and_all_keys(auth):
    user = _member(auth)
    first = auth.create_api_key(user["user_id"], "One", user["user_id"])
    second = auth.create_api_key(user["user_id"], "Two", user["user_id"])
    pending = auth.authenticate_api_key(first["secret"], allow_unverified=True)

    with pytest.raises(AuthError) as exc_info:
        auth.verify_characters(pending, ["Mallory"])

    assert exc_info.value.code == "unauthorized_eve_character"
    assert auth.repository.user_by_id(user["user_id"])["status"] == "disabled"
    assert {item["status"] for item in auth.list_api_keys(user["user_id"])} == {"revoked"}
    with pytest.raises(AuthError):
        auth.authenticate_api_key(second["secret"], allow_unverified=True)


def test_unresolved_character_blocks_without_disabling_user(auth):
    user = _member(auth)
    created = auth.create_api_key(user["user_id"], "Desktop", user["user_id"])
    pending = auth.authenticate_api_key(created["secret"], allow_unverified=True)

    with pytest.raises(AuthError) as exc_info:
        auth.verify_characters(pending, ["Unknown Person"])

    assert exc_info.value.status == 503
    assert auth.repository.user_by_id(user["user_id"])["status"] == "active"


def test_removed_rule_rechecks_saved_characters_and_requires_new_key_after_enable(auth):
    user = _member(auth)
    auth.add_allowed_corporation(9001, user["user_id"])
    created = auth.create_api_key(user["user_id"], "Desktop", user["user_id"])
    pending = auth.authenticate_api_key(created["secret"], allow_unverified=True)
    auth.verify_characters(pending, ["Alice"])

    auth.delete_allowed_corporation(9001, user["user_id"])
    assert auth.repository.user_by_id(user["user_id"])["status"] == "disabled"

    auth.set_user_status(user["user_id"], True, user["user_id"])
    assert auth.repository.user_by_id(user["user_id"])["status"] == "active"
    with pytest.raises(AuthError):
        auth.authenticate_api_key(created["secret"], allow_unverified=True)
