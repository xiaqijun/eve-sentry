#!/usr/bin/env bash
set -Eeuo pipefail

archive="${1:?deployment archive is required}"
revision="${2:?git revision is required}"
backend_root="${3:-/opt/eve-sentry}"
frontend_root="${4:-/opt/1panel/www/eve-sentry}"
health_url="${5:-http://127.0.0.1:8765/api/health}"
service_name="${6:-eve-sentry}"

if [[ ! "$revision" =~ ^[0-9a-f]{40}$ ]]; then
    echo "Invalid git revision: $revision" >&2
    exit 2
fi
if [[ ! -f "$archive" ]]; then
    echo "Deployment archive is missing: $archive" >&2
    exit 2
fi

lock_file="/var/lock/eve-sentry-deploy.lock"
exec 9>"$lock_file"
flock -w 300 9 || {
    echo "Another EVE Sentry deployment is still running." >&2
    exit 3
}

timestamp="$(date +%Y%m%d-%H%M%S)"
staging="$(mktemp -d /tmp/eve-sentry-deploy.XXXXXX)"
backup_root="$backend_root/.deploy-backups"
backup="$backup_root/$timestamp-$revision"
service_file="/etc/systemd/system/$service_name.service"
managed_directories=(app scripts deploy resources)
managed_files=(main.py requirements-server.txt intel_map.json)
deployment_started=0
deployment_complete=0

restore_backup() {
    echo "Deployment failed; restoring $backup" >&2
    for name in "${managed_directories[@]}"; do
        rm -rf "$backend_root/$name"
        if [[ -d "$backup/backend/$name" ]]; then
            cp -a "$backup/backend/$name" "$backend_root/$name"
        fi
    done
    for name in "${managed_files[@]}"; do
        rm -f "$backend_root/$name"
        if [[ -f "$backup/backend/$name" ]]; then
            cp -a "$backup/backend/$name" "$backend_root/$name"
        fi
    done
    rsync -a --delete "$backup/frontend/" "$frontend_root/"
    if [[ -f "$backup/service/eve-sentry.service" ]]; then
        install -m 0644 "$backup/service/eve-sentry.service" "$service_file"
    fi
    chown -R eve-sentry:eve-sentry "$backend_root/app" "$backend_root/scripts" \
        "$backend_root/deploy" "$backend_root/resources" \
        "$backend_root/main.py" "$backend_root/requirements-server.txt" \
        "$backend_root/intel_map.json" 2>/dev/null || true
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

mkdir -p "$backend_root" "$frontend_root" "$backup/backend" "$backup/frontend" "$backup/service"
tar -xzf "$archive" -C "$staging"
test -f "$staging/backend/app/server/__main__.py"
test -f "$staging/backend/scripts/run_server.py"
test -f "$staging/backend/requirements-server.txt"
test -f "$staging/backend/deploy/linux/eve-sentry.service"
test -s "$staging/frontend/index.html"

for name in "${managed_directories[@]}"; do
    if [[ -d "$backend_root/$name" ]]; then
        cp -a "$backend_root/$name" "$backup/backend/$name"
    fi
done
for name in "${managed_files[@]}"; do
    if [[ -f "$backend_root/$name" ]]; then
        cp -a "$backend_root/$name" "$backup/backend/$name"
    fi
done
rsync -a "$frontend_root/" "$backup/frontend/"
if [[ -f "$service_file" ]]; then
    cp -a "$service_file" "$backup/service/eve-sentry.service"
fi

deployment_started=1
for name in "${managed_directories[@]}"; do
    mkdir -p "$backend_root/$name"
    rsync -a --delete "$staging/backend/$name/" "$backend_root/$name/"
done
for name in "${managed_files[@]}"; do
    install -m 0644 "$staging/backend/$name" "$backend_root/$name"
done
rsync -a --delete "$staging/frontend/" "$frontend_root/"
install -m 0644 "$staging/backend/deploy/linux/eve-sentry.service" "$service_file"

chown -R eve-sentry:eve-sentry "$backend_root/app" "$backend_root/scripts" \
    "$backend_root/deploy" "$backend_root/resources" \
    "$backend_root/main.py" "$backend_root/requirements-server.txt" \
    "$backend_root/intel_map.json"
runuser -u eve-sentry -- "$backend_root/.venv-server/bin/python" -m pip install \
    --disable-pip-version-check -r "$backend_root/requirements-server.txt"

systemctl daemon-reload
systemctl restart "$service_name"

healthy=0
for _ in $(seq 1 30); do
    if curl -fsS --max-time 5 "$health_url" >/dev/null; then
        healthy=1
        break
    fi
    sleep 2
done
if [[ "$healthy" -ne 1 ]]; then
    journalctl -u "$service_name" -n 100 --no-pager >&2 || true
    echo "Health check failed after deployment." >&2
    exit 1
fi

printf '%s\n' "$revision" > /var/lib/eve-sentry/deployed-revision
chown eve-sentry:eve-sentry /var/lib/eve-sentry/deployed-revision
deployment_complete=1

ls -1dt "$backup_root"/* 2>/dev/null | tail -n +6 | xargs -r rm -rf --
echo "DEPLOYED_REVISION=$revision"
echo "BACKUP=$backup"
echo "HEALTH_URL=$health_url"
