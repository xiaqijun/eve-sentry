"""Restore and persist the existing QQ bot Redis account from a protected job."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import time
from urllib.parse import unquote, urlsplit
from urllib.request import urlopen

from dotenv import dotenv_values
from redis import Redis


USERNAME = "eve_risk"


def replace_user(text: str, rule: str) -> str:
    """Replace only the bot's rule, leaving every other directive untouched."""
    lines = []
    for line in text.splitlines(keepends=True):
        # A password hash starts with #, so shlex comments must stay disabled.
        tokens = shlex.split(line, comments=False) if not line.lstrip().startswith("#") else []
        if tokens[:2] != ["user", USERNAME]:
            lines.append(line)
    prefix = "".join(lines)
    return prefix + ("\n" if prefix and not prefix.endswith("\n") else "") + rule + "\n"


def write_config(path: Path, data: bytes) -> None:
    """Keep the inode: Redis may be using a single-file Docker bind mount."""
    with path.open("r+b") as stream:
        stream.write(data)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())


def probe(client: Redis) -> dict[str, object]:
    if not client.ping():
        raise RuntimeError("Redis PING failed")
    return {
        "subscribed_groups": client.scard("qq:eve-sentry:alert-groups"),
        "has_status_state": bool(client.exists("qq:eve-server:status-state")),
    }


def restore_account(
    admin: Redis, client: Redis, password: str, path: Path, *, apply: bool = False,
    verify=None,
) -> dict[str, object]:
    """Persist the account and roll back both disk and ACL on any failure."""
    original = path.read_bytes()
    previous = next(
        (line for line in admin.acl_list() if shlex.split(line)[:2] == ["user", USERNAME]),
        None,
    )
    if previous:
        # An existing account must authenticate as configured. Never reset its
        # password or broaden its permissions just to make a health check pass.
        probe(client)
        rule = previous
    else:
        digest = hashlib.sha256(password.encode()).hexdigest()
        # Match the former bot deployment's grants; do not change other users.
        rule = f"user {USERNAME} on #{digest} ~* +@all"
        existing = [
            shlex.split(line, comments=False)
            for line in original.decode().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if any(tokens[:2] == ["user", USERNAME] for tokens in existing):
            raise RuntimeError("Bot has a persisted rule but is absent at runtime; investigate first")
    updated = replace_user(original.decode(), rule).encode()
    if not apply:
        return {"account_exists": bool(previous), "persistence_change": updated != original}
    backup = path.with_name(path.name + ".qq-bot-backup-" + str(time.time_ns()))
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(original)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        if updated != original:
            write_config(path, updated)
        if not previous:
            admin.execute_command("ACL", "SETUSER", USERNAME, "reset", *shlex.split(rule)[2:])
        result = probe(client)
        if verify is not None:
            verify()
    except BaseException:
        write_config(path, original)
        if not previous:
            admin.acl_deluser(USERNAME)
        raise
    return {**result, "account_exists": True, "persistent": True, "backup": str(backup)}


def bind_source(container: dict, target: str) -> Path:
    target_path = Path(target)
    for mount in sorted(container["Mounts"], key=lambda m: len(m["Destination"]), reverse=True):
        destination = Path(mount["Destination"])
        if target_path == destination or destination in target_path.parents:
            if mount["Type"] != "bind" or not mount["RW"]:
                raise RuntimeError("Redis configuration needs a writable persistent bind mount")
            base = Path(mount["Source"]).resolve()
            source = base / target_path.relative_to(destination)
            resolved = source.resolve()
            if resolved != base and base not in resolved.parents:
                raise RuntimeError("Redis configuration escapes its bind mount")
            if not resolved.is_file():
                raise RuntimeError("Redis persistent configuration was not found")
            return resolved
    raise RuntimeError("Redis configuration is not on a persistent bind mount")


def deployment_clients(environment: Path):
    values = dotenv_values(environment, interpolate=False)
    value = values.get("EVE_SENTRY_SERVER_QQ_BOT_REDIS_URL") or ""
    url = urlsplit(value)
    if url.scheme != "redis" or unquote(url.username or "") != USERNAME or not url.password:
        raise RuntimeError("Expected the existing dedicated bot Redis URL")
    ids = subprocess.check_output(["docker", "ps", "-q"], text=True).split()
    if not ids:
        raise RuntimeError("No Redis container is running")
    containers = json.loads(subprocess.check_output(["docker", "inspect", *ids], text=True))
    matching = [
        container for container in containers
        if url.hostname in {
            network.get("IPAddress")
            for network in container["NetworkSettings"]["Networks"].values()
        }
    ]
    if len(matching) != 1 or (url.port or 6379) != 6379:
        raise RuntimeError("Bot endpoint must identify exactly one local Redis container")
    container = matching[0]
    command = container["Config"].get("Cmd") or []
    if "--requirepass" not in command:
        raise RuntimeError("Redis administrator password was not found in the container command")
    admin = Redis(
        host=url.hostname, port=6379,
        password=command[command.index("--requirepass") + 1],
        decode_responses=True, socket_timeout=5, socket_connect_timeout=5,
    )
    client = Redis.from_url(value, decode_responses=True, socket_timeout=5, socket_connect_timeout=5)
    try:
        config = admin.info("server")["config_file"]
        aclfile = admin.config_get("aclfile")["aclfile"]
        if aclfile and not Path(aclfile).is_absolute():
            raise RuntimeError("Relative ACL file paths need manual review")
        path = bind_source(container, aclfile or config)
        if not aclfile:
            for line in path.read_text().splitlines():
                if line.lstrip().startswith("#") or not line.strip():
                    continue
                tokens = shlex.split(line, comments=False)
                if tokens[0] == "include":
                    raise RuntimeError("Redis includes need manual review before changing ACL rules")
        return admin, client, unquote(url.password), path
    except BaseException:
        admin.close()
        client.close()
        raise


def health() -> None:
    with urlopen("http://127.0.0.1:8765/api/readyz", timeout=5) as response:
        if not json.load(response).get("ok"):
            raise RuntimeError("Warning server is not ready")


def restart_and_verify() -> None:
    subprocess.run(["systemctl", "restart", "eve-sentry"], check=True, timeout=60)
    for attempt in range(30):
        try:
            health()
            return
        except Exception:
            if attempt == 29:
                raise
            time.sleep(2)


def main() -> None:
    import fcntl

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    with Path("/var/lock/eve-sentry-deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        admin, client, password, path = deployment_clients(Path("/etc/eve-sentry/eve-sentry.env"))
        try:
            health()
            result = restore_account(
                admin, client, password, path, apply=args.apply,
                verify=restart_and_verify if args.apply else None,
            )
            print(json.dumps(result))
        finally:
            admin.close()
            client.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Never log URLs, Docker command arguments, ACL hashes, or passwords.
        raise SystemExit(f"QQ Redis repair failed ({type(error).__name__}); configuration rolled back if changed") from None
