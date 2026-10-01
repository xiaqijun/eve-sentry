#!/usr/bin/env bash
set -Eeuo pipefail

archive="${1:?deployment archive is required}"
revision="${2:?git revision is required}"
backend_root="${3:-/opt/eve-sentry}"
frontend_root="${4:-/opt/1panel/www/eve-sentry}"
health_url="${5:-http://127.0.0.1:8765/api/readyz}"
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
managed_directories=(app scripts deploy bot)
managed_files=(requirements-server.txt intel_map.json)
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
        "$backend_root/bot" \
        "$backend_root/deploy" "$backend_root/requirements-server.txt" \
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
test -f "$staging/backend/bot/src/eve_risk/bot.py"
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

# The former standalone bot kept its credentials in the risk-analysis
# deployment.  Import them once into the server-scoped environment before the
# embedded runtime starts.  Values are never echoed to the deployment log.
legacy_bot_env="/opt/eve-risk-analysis/.runtime.env"
server_env="/etc/eve-sentry/eve-sentry.env"
if [[ -r "$legacy_bot_env" ]]; then
    python3 - "$legacy_bot_env" "$server_env" "$backend_root" <<'PY'
import os
import sys
from pathlib import Path


legacy_path = Path(sys.argv[1])
server_path = Path(sys.argv[2])
backend_root = sys.argv[3]


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


legacy = parse_env(legacy_path)
required = {
    "QQ_APP_ID": "EVE_SENTRY_SERVER_QQ_BOT_APP_ID",
    "QQ_APP_SECRET": "EVE_SENTRY_SERVER_QQ_BOT_APP_SECRET",
    "DATABASE_URL": "EVE_SENTRY_SERVER_QQ_BOT_DATABASE_URL",
    "REDIS_URL": "EVE_SENTRY_SERVER_QQ_BOT_REDIS_URL",
    "EVE_SENTRY_PUBLIC_URL": "EVE_SENTRY_SERVER_QQ_BOT_PUBLIC_URL",
    "EVE_SENTRY_ALERT_MIN_LEVEL": "EVE_SENTRY_SERVER_QQ_BOT_ALERT_MIN_LEVEL",
}
updates = {
    target: legacy[source]
    for source, target in required.items()
    if legacy.get(source, "")
}
if legacy.get("QQ_APP_ID") and legacy.get("QQ_APP_SECRET"):
    updates["EVE_SENTRY_SERVER_QQ_BOT_ENABLED"] = "1"
    updates["EVE_SENTRY_SERVER_QQ_BOT_SOURCE"] = f"{backend_root}/bot/src"

if not updates:
    print("No legacy QQ bot configuration found to import.")
    raise SystemExit(0)

server_path.parent.mkdir(parents=True, exist_ok=True)
existed = server_path.exists()
old_mode = server_path.stat().st_mode & 0o777 if existed else 0o640
lines = server_path.read_text(encoding="utf-8").splitlines() if existed else []
seen: set[str] = set()
for index, line in enumerate(lines):
    key, separator, _ = line.partition("=")
    if not separator or key.strip() not in updates:
        continue
    normalized_key = key.strip()
    lines[index] = f"{normalized_key}={updates[normalized_key]}"
    seen.add(normalized_key)
for key, value in updates.items():
    if key not in seen:
        lines.append(f"{key}={value}")
server_path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
os.chmod(server_path, old_mode)
print("Imported legacy QQ bot configuration into the server environment.")
PY
fi

chown -R eve-sentry:eve-sentry "$backend_root/app" "$backend_root/scripts" \
    "$backend_root/bot" \
    "$backend_root/deploy" "$backend_root/requirements-server.txt" \
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
    echo "Readiness check failed after deployment." >&2
    exit 1
fi

printf '%s\n' "$revision" > /var/lib/eve-sentry/deployed-revision
chown eve-sentry:eve-sentry /var/lib/eve-sentry/deployed-revision
deployment_complete=1

ls -1dt "$backup_root"/* 2>/dev/null | tail -n +6 | xargs -r rm -rf --
echo "DEPLOYED_REVISION=$revision"
echo "BACKUP=$backup"
echo "HEALTH_URL=$health_url"
