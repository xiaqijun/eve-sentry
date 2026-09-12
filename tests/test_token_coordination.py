"""Deterministic concurrent refresh and authorization replacement fences."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from types import SimpleNamespace

import pytest

from app.esi.session import EsiAuthenticatedSession
from app.esi.sso import EsiSsoError, EsiTokenStore, TokenSet


def test_shared_path_only_refreshes_once(tmp_path):
    stores = [EsiTokenStore(tmp_path / "token.json") for _ in range(8)]
    old = TokenSet("old", refresh_token="refresh", expires_at=1)
    new = replace(old, access_token="new", expires_at=5000)
    stores[0].save(old)
    calls = []

    def refresh(_):
        calls.append(1)
        return new

    sessions = [EsiAuthenticatedSession(SimpleNamespace(refresh=refresh), token_store=s) for s in stores]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda s: s.refresh_tokens(old), sessions))
    assert results == [new] * 8
    assert calls == [1]
    assert stores[0].load() == new


@pytest.mark.parametrize("logout", [False, True])
def test_login_or_logout_during_refresh_wins(tmp_path, logout):
    store = EsiTokenStore(tmp_path / "token.json")
    old = TokenSet("old", refresh_token="refresh", expires_at=1)
    store.save(old)
    entered, release = Event(), Event()

    def refresh(_):
        entered.set()
        assert release.wait(5)
        return replace(old, access_token="late")

    session = EsiAuthenticatedSession(SimpleNamespace(refresh=refresh), token_store=store)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(session.refresh_tokens, old)
        assert entered.wait(5)
        if logout:
            store.clear()
        else:
            store.save(replace(old, access_token="new-login"))
        release.set()
        with pytest.raises(EsiSsoError, match="authorization changed"):
            result.result()
    assert store.load() is None if logout else store.load().access_token == "new-login"


def test_same_token_relogin_is_still_new_revision(tmp_path):
    store = EsiTokenStore(tmp_path / "token.json")
    old = TokenSet("old", expires_at=5000)
    store.save(old)
    current, revision = store.load_versioned()
    store.clear()
    store.save(old)
    assert not store.save_if_unchanged(replace(old, access_token="late"), previous=current, revision=revision)
    assert store.load() == old
