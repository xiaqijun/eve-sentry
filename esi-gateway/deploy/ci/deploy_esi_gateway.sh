#!/usr/bin/env bash
set -Eeuo pipefail

archive="${1:?deployment archive is required}"
revision="${2:?git revision is required}"
expected_sha256="${3:?artifact SHA-256 is required}"
gateway_root="${4:-/opt/eve-sentry-esi-gateway}"
service_name="${5:-eve-sentry-esi-gateway}"
health_url="${6:-http://10.233.53.17:8787/health}"
keep_releases="${7:-5}"
unit_dir="${SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
health_attempts="${DEPLOY_HEALTH_ATTEMPTS:-30}"
health_delay="${DEPLOY_HEALTH_DELAY:-2}"

fail() {
    echo "ERROR: $*" >&2
    exit 2
}

[[ "$revision" =~ ^[0-9a-f]{40}$ ]] || fail "Invalid git revision: $revision"
[[ "$expected_sha256" =~ ^[0-9a-f]{64}$ ]] || fail "Invalid artifact SHA-256"
[[ -f "$archive" ]] || fail "Deployment archive is missing: $archive"
staging=""
cleanup() {
    if [[ -n "$staging" ]]; then
        rm -rf -- "$staging"
    fi
    rm -f -- "$archive"
    if [[ "$0" == /tmp/eve-sentry-esi-gateway-deploy-*.sh ]]; then
        rm -f -- "$0"
    fi
}
trap cleanup EXIT

[[ "$gateway_root" =~ ^/[A-Za-z0-9._/-]+$ && "$gateway_root" != "/" && "$gateway_root" != *".."* ]] || fail "Invalid gateway root: $gateway_root"
[[ "$service_name" =~ ^[A-Za-z0-9_.@-]+$ ]] || fail "Invalid service name: $service_name"
[[ "$health_url" =~ ^https?://[^[:space:]]+$ ]] || fail "Invalid health URL: $health_url"
if [[ ! "$keep_releases" =~ ^[0-9]+$ ]] || (( keep_releases < 2 || keep_releases > 20 )); then
    fail "keep_releases must be between 2 and 20"
fi
if [[ ! "$health_attempts" =~ ^[0-9]+$ ]] || (( health_attempts < 1 )); then
    fail "DEPLOY_HEALTH_ATTEMPTS must be positive"
fi
[[ "$health_delay" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail "DEPLOY_HEALTH_DELAY must be non-negative"
if [[ "$unit_dir" == "/etc/systemd/system" && "$EUID" -ne 0 ]]; then
    fail "Deployment must run as root when installing a system service"
fi

for command in curl flock install journalctl python3 sed sha256sum systemctl tar; do
    command -v "$command" >/dev/null || fail "Required command is missing: $command"
done

actual_sha256="$(sha256sum "$archive" | awk '{print $1}')"
[[ "$actual_sha256" == "$expected_sha256" ]] || fail "Artifact checksum mismatch"

python3 - "$archive" <<'PY'
import pathlib
import sys
import tarfile

with tarfile.open(sys.argv[1], "r:gz") as archive:
    for member in archive.getmembers():
        path = pathlib.PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise SystemExit(f"Unsafe archive path: {member.name}")
        if member.issym() or member.islnk():
            raise SystemExit(f"Archive links are not allowed: {member.name}")
PY

install -d "$gateway_root" "$gateway_root/releases" "$unit_dir"
runtime_python="$gateway_root/.venv/bin/python"
[[ -x "$runtime_python" ]] || fail "Runtime Python is missing or not executable: $runtime_python"
exec 9>"$gateway_root/.deploy.lock"
flock -n 9 || fail "Another deployment is already running"

staging="$(mktemp -d "$gateway_root/.staging.XXXXXX")"

tar -xzf "$archive" -C "$staging"
backend="$staging/backend"
manifest="$staging/manifest.json"
test -f "$backend/esi_gateway/client.py" || fail "Artifact is missing esi_gateway/client.py"
test -f "$backend/scripts/esi_gateway.py" || fail "Artifact is missing scripts/esi_gateway.py"
test -f "$backend/deploy/linux/eve-sentry-esi-gateway.service" || fail "Artifact is missing the systemd unit"
test -f "$backend/pyproject.toml" || fail "Artifact is missing pyproject.toml"
test -f "$manifest" || fail "Artifact is missing manifest.json"
"$runtime_python" -m compileall -q "$backend/esi_gateway" "$backend/scripts"
python3 - "$manifest" "$revision" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    manifest = json.load(stream)
if manifest.get("artifact") != "eve-sentry-esi-gateway":
    raise SystemExit("Unexpected artifact name")
if manifest.get("revision") != sys.argv[2]:
    raise SystemExit("Artifact revision does not match requested revision")
if manifest.get("layout_version") != 2:
    raise SystemExit("Unsupported artifact layout")
PY

release_dir="$gateway_root/releases/$revision"
release_created=false
if [[ -d "$release_dir" ]]; then
    stored_sha256="$(<"$release_dir/.artifact-sha256")"
    [[ "$stored_sha256" == "$expected_sha256" ]] || fail "Existing release checksum does not match artifact"
else
    install -m 0644 "$manifest" "$backend/manifest.json"
    printf '%s\n' "$expected_sha256" > "$backend/.artifact-sha256"
    mv "$backend" "$release_dir"
    release_created=true
fi

unit_path="$unit_dir/$service_name.service"
unit_backup="$staging/previous.service"
unit_existed=false
if [[ -f "$unit_path" ]]; then
    cp -a "$unit_path" "$unit_backup"
    unit_existed=true
fi

previous_release=""
if [[ -L "$gateway_root/current" ]]; then
    previous_release="$(readlink -f "$gateway_root/current")"
elif [[ -e "$gateway_root/current" ]]; then
    fail "$gateway_root/current exists but is not a symbolic link"
fi

rollback() {
    echo "Deployment failed; restoring the previous release." >&2
    if [[ -n "$previous_release" ]]; then
        ln -sfn "$previous_release" "$gateway_root/.current.rollback"
        mv -Tf "$gateway_root/.current.rollback" "$gateway_root/current"
    else
        rm -f -- "$gateway_root/current"
    fi
    if [[ "$unit_existed" == true ]]; then
        install -m 0644 "$unit_backup" "$unit_path"
        systemctl daemon-reload || true
        systemctl restart "$service_name" || true
    else
        systemctl stop "$service_name" || true
        rm -f -- "$unit_path"
        systemctl daemon-reload || true
    fi
    journalctl -u "$service_name" -n 100 --no-pager >&2 || true
    echo "ROLLBACK_RELEASE=${previous_release:-legacy-layout}" >&2
}

rendered_unit="$staging/$service_name.service"
sed "s#/opt/eve-sentry-esi-gateway#$gateway_root#g" \
    "$release_dir/deploy/linux/eve-sentry-esi-gateway.service" > "$rendered_unit"
install -m 0644 "$rendered_unit" "$unit_path"
ln -sfn "$release_dir" "$gateway_root/.current.$revision"
mv -Tf "$gateway_root/.current.$revision" "$gateway_root/current"
systemctl daemon-reload
if ! systemctl restart "$service_name"; then
    rollback
    exit 1
fi

healthy=false
for ((attempt = 1; attempt <= health_attempts; attempt++)); do
    if curl -fsS --max-time 5 "$health_url" | python3 -c 'import json,sys; payload=json.load(sys.stdin); raise SystemExit(0 if payload.get("ok") is True else 1)'; then
        healthy=true
        break
    fi
    echo "Health check attempt ${attempt}/${health_attempts} failed." >&2
    sleep "$health_delay"
done

if [[ "$healthy" != true ]]; then
    rollback
    echo "ESI Gateway health check failed after deployment." >&2
    exit 1
fi

revision_tmp="$(mktemp "$gateway_root/.deployed-revision.XXXXXX")"
printf '%s\n' "$revision" > "$revision_tmp"
mv -f "$revision_tmp" "$gateway_root/deployed-revision"

mapfile -t releases < <(find "$gateway_root/releases" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %f\n' | sort -nr | awk '{print $2}')
kept=1
for release in "${releases[@]}"; do
    [[ "$release" =~ ^[0-9a-f]{40}$ ]] || continue
    if [[ "$release" == "$revision" ]]; then
        continue
    fi
    if [[ "$kept" -lt "$keep_releases" ]]; then
        ((kept += 1))
        continue
    fi
    rm -rf -- "$gateway_root/releases/$release"
done

echo "DEPLOYED_REVISION=$revision"
echo "DEPLOYED_RELEASE=$release_dir"
echo "PREVIOUS_RELEASE=${previous_release:-legacy-layout}"
echo "RELEASE_CREATED=$release_created"
