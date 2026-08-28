#!/usr/bin/env bash
set -Eeuo pipefail

archive="${1:?deployment archive is required}"
revision="${2:?git revision is required}"
gateway_root="${3:-/opt/eve-sentry-esi-gateway}"
service_name="${4:-eve-sentry-esi-gateway}"
health_url="${5:-http://10.233.53.17:8787/health}"

if [[ ! "$revision" =~ ^[0-9a-f]{40}$ ]]; then
    echo "Invalid git revision: $revision" >&2
    exit 2
fi
if [[ ! -f "$archive" ]]; then
    echo "Deployment archive is missing: $archive" >&2
    exit 2
fi

lock_file="/var/lock/eve-sentry-esi-gateway-deploy.lock"
exec 9>"$lock_file"
flock -w 300 9 || {
    echo "Another ESI Gateway deployment is still running." >&2
    exit 3
}

timestamp="$(date +%Y%m%d-%H%M%S)"
staging="$(mktemp -d /tmp/eve-sentry-esi-gateway-deploy.XXXXXX)"
backup_root="$gateway_root/.deploy-backups"
backup="$backup_root/$timestamp-$revision"
service_file="/etc/systemd/system/$service_name.service"
deployment_started=0
deployment_complete=0

restore_backup() {
    echo "Deployment failed; restoring $backup" >&2
    rm -rf "$gateway_root/app/esi" "$gateway_root/scripts/esi_gateway.py"
    if [[ -d "$backup/app/esi" ]]; then
        mkdir -p "$gateway_root/app"
        cp -a "$backup/app/esi" "$gateway_root/app/esi"
    fi
    if [[ -f "$backup/scripts/esi_gateway.py" ]]; then
        mkdir -p "$gateway_root/scripts"
        cp -a "$backup/scripts/esi_gateway.py" "$gateway_root/scripts/esi_gateway.py"
    fi
    if [[ -f "$backup/service/$service_name.service" ]]; then
        install -m 0644 "$backup/service/$service_name.service" "$service_file"
    fi
    chown -R eve-sentry-esi:eve-sentry-esi "$gateway_root/app/esi" \
        "$gateway_root/scripts/esi_gateway.py" 2>/dev/null || true
    systemctl daemon-reload
    systemctl restart "$service_name"
}

finish() {
    status=$?
    trap - EXIT
    if [[ "$status" -ne 0 && "$deployment_started" -eq 1 && "$deployment_complete" -eq 0 ]]; then
        restore_backup || true
    fi
    rm -rf "$staging" "$archive"
    exit "$status"
}
trap finish EXIT

mkdir -p "$gateway_root" "$backup_root/$timestamp-$revision/app" \
    "$backup_root/$timestamp-$revision/scripts" "$backup_root/$timestamp-$revision/service"
tar -xzf "$archive" -C "$staging"
test -f "$staging/backend/app/esi/client.py"
test -f "$staging/backend/scripts/esi_gateway.py"
test -f "$staging/backend/deploy/linux/eve-sentry-esi-gateway.service"
if [[ -d "$gateway_root/app/esi" ]]; then cp -a "$gateway_root/app/esi" "$backup/app/esi"; fi
if [[ -f "$gateway_root/scripts/esi_gateway.py" ]]; then cp -a "$gateway_root/scripts/esi_gateway.py" "$backup/scripts/esi_gateway.py"; fi
if [[ -f "$service_file" ]]; then cp -a "$service_file" "$backup/service/$service_name.service"; fi

deployment_started=1
mkdir -p "$gateway_root/app" "$gateway_root/scripts"
rsync -a --delete "$staging/backend/app/esi/" "$gateway_root/app/esi/"
install -m 0755 "$staging/backend/scripts/esi_gateway.py" "$gateway_root/scripts/esi_gateway.py"
install -m 0644 "$staging/backend/deploy/linux/eve-sentry-esi-gateway.service" "$service_file"
chown -R eve-sentry-esi:eve-sentry-esi "$gateway_root/app/esi" \
    "$gateway_root/scripts/esi_gateway.py"
systemctl daemon-reload
systemctl restart "$service_name"

healthy=0
for _ in $(seq 1 30); do
    if curl -fsS --max-time 5 "$health_url" > "$staging/health.json"; then
        if python3 - "$staging/health.json" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
if payload.get("ok") is not True or "cache_entries" not in payload:
    raise SystemExit(1)
PY
        then
            healthy=1
            break
        fi
    fi
    sleep 2
done
if [[ "$healthy" -ne 1 ]]; then
    journalctl -u "$service_name" -n 100 --no-pager >&2 || true
    echo "ESI Gateway health check failed after deployment." >&2
    exit 1
fi

printf '%s\n' "$revision" > "$gateway_root/deployed-revision"
chown eve-sentry-esi:eve-sentry-esi "$gateway_root/deployed-revision"
deployment_complete=1
ls -1dt "$backup_root"/* 2>/dev/null | tail -n +6 | xargs -r rm -rf --
echo "DEPLOYED_REVISION=$revision"
echo "HEALTH_URL=$health_url"
