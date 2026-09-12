"""Protected-workflow-only ESI transport switch with preflight and rollback."""

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

TARGETS = {
    "server": (Path("/etc/eve-sentry/eve-sentry.env"), "eve-sentry",
               "EVE_SENTRY_ESI_TRANSPORT", "http://127.0.0.1:8765/api/readyz",
               Path("/var/lock/eve-sentry-deploy.lock")),
    "gateway": (Path("/etc/eve-sentry-esi/gateway.env"), "eve-sentry-esi-gateway",
                "EVE_SENTRY_ESI_GATEWAY_TRANSPORT_MODE", "http://10.233.53.17:8787/health",
                Path("/opt/eve-sentry-esi-gateway/.deploy.lock")),
}


def environment(text):
    result = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise ValueError("unsupported environment file syntax")
        parsed = shlex.split(value, comments=False)
        result[key.strip()] = " ".join(parsed)
    return result


def replace_mode(text, key, mode):
    lines = [line for line in text.splitlines() if line.partition("=")[0].strip() != key]
    return "\n".join([*lines, f"{key}={mode}", ""])


def atomic_write(path, content, stat):
    fd, temporary = tempfile.mkstemp(prefix=".transport-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), stat.st_mode & 0o777)
            os.fchown(stream.fileno(), stat.st_uid, stat.st_gid)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def health(url, *, expected_mode=None):
    with urlopen(url, timeout=5) as response:
        payload = json.load(response)
    if not payload.get("ok") or (expected_mode and payload.get("transport_mode", "legacy") != expected_mode):
        raise RuntimeError("transport readiness check failed")


def preflight(component, mode, values):
    if component == "gateway":
        sys.path.insert(0, "/opt/eve-sentry-esi-gateway/current")
        from esi_gateway.relay import validate_relay_binding
        if mode != "legacy":
            validate_relay_binding(values.get("EVE_SENTRY_ESI_GATEWAY_HOST", ""),
                                   set(values.get("EVE_SENTRY_ESI_GATEWAY_ALLOWED_CLIENTS", "").replace(",", " ").split()))
            if len(values.get("EVE_SENTRY_ESI_GATEWAY_TOKEN", "")) < 32:
                raise RuntimeError("gateway credential not configured")
        return
    sys.path.insert(0, "/opt/eve-sentry")
    from app.esi.transport import TransportEsiClient, configured_connections
    from app.esi.remote import RemoteEsiClient
    url = values.get("EVE_SENTRY_SERVER_ESI_GATEWAY_URL", "")
    token = values.get("EVE_SENTRY_SERVER_ESI_GATEWAY_TOKEN", "")
    client = (TransportEsiClient(configured_connections("relay", url, token), timeout=15)
              if mode == "relay" else RemoteEsiClient(url, gateway_token=token, timeout=15))
    # One normal public query; no cache bypass or alternate exit retry.
    if client.get_system(30000142).get("name") != "Jita":
        raise RuntimeError("ESI route probe failed")


def switch(component, mode, *, apply=False):
    import fcntl
    allowed = {"server": {"legacy", "relay"}, "gateway": {"legacy", "dual", "relay"}}
    if component not in allowed or mode not in allowed[component]:
        raise ValueError("invalid transport target")
    path, service, key, url, lock_path = TARGETS[component]
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        original, stat = path.read_bytes(), path.stat()
        values = environment(original.decode())
        health(url)
        preflight(component, mode, values)
        if not apply:
            print(f"Preflight passed: {component} -> {mode}; configuration unchanged")
            return
        updated = replace_mode(original.decode(), key, mode).encode()
        if updated == original:
            health(url, expected_mode=mode if component == "gateway" else None)
            print(f"Already configured: {component}={mode}")
            return
        backup = path.with_name(path.name + ".transport-backup-" + str(time.time_ns()))
        atomic_write(backup, original, stat)
        try:
            atomic_write(path, updated, stat)
            subprocess.run(["systemctl", "restart", service], check=True, timeout=45)
            for attempt in range(20):
                try:
                    health(url, expected_mode=mode if component == "gateway" else None)
                    break
                except Exception:
                    if attempt == 19:
                        raise
                    time.sleep(2)
        except BaseException:
            atomic_write(path, original, stat)
            subprocess.run(["systemctl", "restart", service], check=True, timeout=45)
            print("Previous transport configuration restored", file=sys.stderr)
            raise
        print(f"Transport verified: {component}={mode}; backup={backup.name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=TARGETS)
    parser.add_argument("mode", choices=("legacy", "dual", "relay"))
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args()
    try:
        switch(arguments.component, arguments.mode, apply=arguments.apply)
    except Exception as error:
        # Exceptions may contain upstream URLs; never emit credential-bearing data.
        raise SystemExit(f"Transport switch failed ({type(error).__name__}); inspect protected service logs") from None
