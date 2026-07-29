"""Prepare and sign Ed25519 update manifests for release builds."""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.updater import canonical_manifest_bytes


def load_private_key() -> Ed25519PrivateKey:
    encoded = os.environ.get("EVE_SENTRY_UPDATE_SIGNING_PRIVATE_KEY_B64", "").strip()
    if not encoded:
        raise RuntimeError("EVE_SENTRY_UPDATE_SIGNING_PRIVATE_KEY_B64 is required")
    key = serialization.load_pem_private_key(base64.b64decode(encoded), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise RuntimeError("update signing key must be Ed25519")
    return key


def prepare_public_key(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(
        load_private_key().public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )


def sign_manifest(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["signature_algorithm"] = "ed25519"
    payload["signing_key_id"] = "eve-sentry-release-v1"
    payload["signature"] = base64.b64encode(
        load_private_key().sign(canonical_manifest_bytes(payload))
    ).decode("ascii")
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare-public")
    prepare.add_argument("output", type=Path)
    sign = subparsers.add_parser("sign")
    sign.add_argument("manifest", type=Path)
    args = parser.parse_args()
    if args.command == "prepare-public":
        prepare_public_key(args.output)
    else:
        sign_manifest(args.manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
