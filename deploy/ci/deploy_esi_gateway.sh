#!/usr/bin/env bash
set -Eeuo pipefail

archive="${1:?deployment archive is required}"
revision="${2:?git revision is required}"
gateway_root="${3:-/opt/eve-sentry-esi-gateway}"
service_name="${4:-eve-sentry-esi-gateway}"
health_url="${5:-http://10.233.53.17:8787/health}"

[[ "$revision" =~ ^[0-9a-f]{40}$ ]] || { echo "Invalid git revision: $revision" >&2; exit 2; }
[[ -f "$archive" ]] || { echo "Deployment archive is missing: $archive" >&2; exit 2; }
staging="$(mktemp -d /tmp/eve-sentry-esi-gateway-deploy.XXXXXX)"
trap 'rm -rf "$staging" "$archive"' EXIT
tar -xzf "$archive" -C "$staging"
test -f "$staging/backend/esi_gateway/client.py"
test -f "$staging/backend/scripts/esi_gateway.py"
mkdir -p "$gateway_root"
rsync -a --delete "$staging/backend/esi_gateway/" "$gateway_root/esi_gateway/"
install -d "$gateway_root/scripts"
install -m 0755 "$staging/backend/scripts/esi_gateway.py" "$gateway_root/scripts/esi_gateway.py"
install -m 0644 "$staging/backend/deploy/linux/eve-sentry-esi-gateway.service" "/etc/systemd/system/$service_name.service"
systemctl daemon-reload
systemctl restart "$service_name"
for _ in $(seq 1 30); do
    if curl -fsS --max-time 5 "$health_url" | python3 -c 'import json,sys; p=json.load(sys.stdin); raise SystemExit(0 if p.get("ok") is True else 1)'; then
        printf '%s\n' "$revision" > "$gateway_root/deployed-revision"
        echo "DEPLOYED_REVISION=$revision"
        exit 0
    fi
    sleep 2
done
journalctl -u "$service_name" -n 100 --no-pager >&2 || true
echo "ESI Gateway health check failed after deployment." >&2
exit 1
