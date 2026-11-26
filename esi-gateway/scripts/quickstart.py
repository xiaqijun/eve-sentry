"""Start a standalone Relay gateway using only the Python standard library."""

from __future__ import annotations

import argparse
import ipaddress
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "EVE_SENTRY_ESI_GATEWAY_"
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def private_ipv4(value: str) -> str:
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Enter an exact private IPv4 address") from None
    if not any(address in block for block in PRIVATE_NETWORKS):
        raise argparse.ArgumentTypeError("Enter an exact private IPv4 address")
    return str(address)


def write_new_config(path: Path, config: dict[str, str]) -> None:
    fd, name = tempfile.mkstemp(prefix=".gateway.env.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            for key, value in config.items():
                file.write(f"{key}={value}\n")
        try:
            os.link(temporary, path)
        except FileExistsError:
            # Another quickstart initialized the file first.
            pass
    finally:
        temporary.unlink(missing_ok=True)


def gateway_config(path: Path, gateway_ip: str, sentry_ip: str) -> dict[str, str]:
    expected = {
        PREFIX + "HOST": gateway_ip,
        PREFIX + "PORT": "8787",
        PREFIX + "ALLOWED_CLIENTS": sentry_ip,
    }
    token_key = PREFIX + "TOKEN"
    if not path.exists():
        write_new_config(path, {**expected, token_key: secrets.token_hex(32)})
    config = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and (key in expected or key == token_key):
            if key in config:
                raise ValueError("Duplicate setting in gateway.env")
            config[key] = value
    if any(config.get(key) != value for key, value in expected.items()):
        raise ValueError("The existing gateway.env uses different addresses or port.")
    if len(config.get(token_key, "")) < 32:
        raise ValueError("The existing gateway.env does not contain a service token.")
    return config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gateway_ip", type=private_ipv4)
    parser.add_argument("sentry_ip", type=private_ipv4)
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        parser.error("ESI Gateway requires Python 3.10 or later")
    if args.gateway_ip == args.sentry_ip:
        parser.error("Gateway and Sentry must use distinct addresses.")
    path = ROOT / "gateway.env"
    try:
        config = gateway_config(path, args.gateway_ip, args.sentry_ip)
    except ValueError as error:
        parser.exit(2, f"{error}\n")
    except OSError:
        parser.exit(2, "Could not prepare gateway.env. Check write access to the checkout.\n")
    print(f"Gateway: http://{args.gateway_ip}:8787\nToken file: {path}", flush=True)
    try:
        return subprocess.call([sys.executable, str(ROOT / "scripts/esi_gateway.py"), "--transport-mode", "relay"],
                               env={**os.environ, **config})
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
