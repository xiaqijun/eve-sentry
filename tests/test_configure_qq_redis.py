"""Regression coverage for durable bot ACL restoration and rollback."""

import hashlib
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
from unittest.mock import Mock

import pytest
from redis import Redis

from deploy.ci import configure_qq_redis as repair


def test_replace_only_bot_rule_preserves_hashes_and_other_configuration():
    text = "# keep comment\nrequirepass example\nuser other on #abc ~* +@all\nuser eve_risk off\n"
    updated = repair.replace_user(text, "user eve_risk on #def ~* +@all")
    assert updated == text.replace("user eve_risk off", "user eve_risk on #def ~* +@all")
    assert repair.replace_user(updated, "user eve_risk on #def ~* +@all") == updated


def test_dry_run_does_not_write_or_create_account(tmp_path):
    path = tmp_path / "redis.conf"
    path.write_text("requirepass example\n")
    admin = Mock()
    admin.acl_list.return_value = ["user default on #abc ~* +@all"]
    result = repair.restore_account(admin, Mock(), "bot-password", path)
    assert result == {"account_exists": False, "persistence_change": True}
    assert path.read_text() == "requirepass example\n"
    assert len(list(tmp_path.iterdir())) == 1
    admin.execute_command.assert_not_called()


def test_failure_rolls_back_config_and_only_new_account(tmp_path):
    path = tmp_path / "redis.conf"
    original = b"requirepass example\nuser other on #abc ~* +@all\n"
    path.write_bytes(original)
    admin = Mock()
    admin.acl_list.return_value = ["user default on #abc ~* +@all"]
    client = Mock()
    client.ping.return_value = True
    client.scard.return_value = 1
    client.exists.return_value = 1
    verify = Mock(side_effect=RuntimeError("health failed"))
    with pytest.raises(RuntimeError, match="health failed"):
        repair.restore_account(admin, client, "bot-password", path, apply=True, verify=verify)
    assert path.read_bytes() == original
    assert next(tmp_path.glob("*.qq-bot-backup-*")).read_bytes() == original
    admin.acl_deluser.assert_called_once_with("eve_risk")


def test_existing_user_retains_permissions_and_is_not_deleted_on_failure(tmp_path):
    path = tmp_path / "redis.conf"
    original = b"requirepass example\n"
    path.write_bytes(original)
    admin = Mock()
    rule = "user eve_risk on #abc ~qq:* resetchannels -@all +ping +get +scard +exists"
    admin.acl_list.return_value = [rule]
    client = Mock()
    client.ping.return_value = True
    client.scard.return_value = 1
    client.exists.return_value = 1
    verify = Mock(side_effect=RuntimeError("health failed"))
    with pytest.raises(RuntimeError, match="health failed"):
        repair.restore_account(admin, client, "bot-password", path, apply=True, verify=verify)
    assert path.read_bytes() == original
    admin.execute_command.assert_not_called()
    admin.acl_deluser.assert_not_called()


def test_existing_invalid_credentials_fail_without_writes(tmp_path):
    path = tmp_path / "redis.conf"
    path.write_text("requirepass example\n")
    admin = Mock()
    admin.acl_list.return_value = ["user eve_risk on #abc ~* +@all"]
    client = Mock()
    client.ping.side_effect = RuntimeError("invalid credentials")
    with pytest.raises(RuntimeError):
        repair.restore_account(admin, client, "different-password", path, apply=True)
    assert len(list(tmp_path.iterdir())) == 1
    admin.execute_command.assert_not_called()


def test_bind_source_requires_persistent_writable_mount(tmp_path):
    path = tmp_path / "redis.conf"
    path.touch()
    container = {"Mounts": [{"Source": str(path), "Destination": str(path), "Type": "bind", "RW": True}]}
    assert repair.bind_source(container, str(path)) == path
    container["Mounts"][0]["RW"] = False
    with pytest.raises(RuntimeError, match="writable"):
        repair.bind_source(container, str(path))


def test_persisted_user_is_not_overwritten_when_runtime_user_is_missing(tmp_path):
    path = tmp_path / "redis.conf"
    path.write_text("user eve_risk on #abc ~qq:* +get\n")
    admin = Mock()
    admin.acl_list.return_value = []
    with pytest.raises(RuntimeError, match="persisted rule"):
        repair.restore_account(admin, Mock(), "bot-password", path, apply=True)
    admin.execute_command.assert_not_called()


def test_account_survives_real_redis_restart_without_changing_other_users(tmp_path):
    executable = shutil.which("redis-server")
    if executable is None:
        if os.environ.get("EVE_SENTRY_REDIS_INTEGRATION") == "1":
            pytest.fail("redis-server is required for the integration job")
        pytest.skip("redis-server is available in the Linux integration job")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    password = "bot:password/%with-special-characters"
    path = tmp_path / "redis.conf"
    other = hashlib.sha256(b"other-password").hexdigest()
    path.write_text(
        f"port {port}\nbind 127.0.0.1\ndir {tmp_path}\nsave \"\"\nappendonly no\n"
        f"requirepass admin-password\nuser other on #{other} ~other:* +ping\n"
    )
    admin = Redis(host="127.0.0.1", port=port, password="admin-password", decode_responses=True)
    bot = Redis(host="127.0.0.1", port=port, username="eve_risk", password=password, decode_responses=True)
    process = None
    try:
        for cycle in range(2):
            process = subprocess.Popen([executable, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for attempt in range(100):
                try:
                    admin.ping()
                    break
                except Exception:
                    if process.poll() is not None or attempt == 99:
                        raise
                    time.sleep(0.05)
            other_before = admin.acl_getuser("other")
            if cycle == 0:
                admin.sadd("qq:eve-sentry:alert-groups", "group-1")
                result = repair.restore_account(admin, bot, password, path, apply=True)
                assert result["subscribed_groups"] == 1
                assert password not in path.read_text()
                assert len(admin.acl_getuser("eve_risk")["passwords"]) == 1
                # Re-running may normalize the Redis ACL spelling but cannot
                # broaden grants, duplicate the user, or remove subscriptions.
                repair.restore_account(admin, bot, password, path, apply=True)
                assert bot.scard("qq:eve-sentry:alert-groups") == 1
            else:
                assert bot.ping()
                assert path.read_text().count("user eve_risk ") == 1
            assert admin.acl_getuser("other") == other_before
            with Redis(host="127.0.0.1", port=port, username="other", password="other-password") as client:
                assert client.ping()
            process.terminate()
            process.wait(timeout=10)
            admin.connection_pool.disconnect()
            bot.connection_pool.disconnect()
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        admin.close()
        bot.close()
