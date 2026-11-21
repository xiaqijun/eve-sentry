#!/usr/bin/env bash
# Temporary source selection for the private ESI gateway during a ZeroTier outage.
set -euo pipefail
export LC_ALL=C

source_ip=10.233.53.204
gateway_ip=10.233.53.17
rescue_peer=10.233.54.17
rescue_interface=ztvdldagjl
table=18787
route=("$gateway_ip/32" via "$rescue_peer" dev "$rescue_interface" src "$source_ip" onlink table "$table")
rule=(priority "$table" from all to "$gateway_ip/32" iif lo ipproto tcp dport 8787 lookup "$table")

exec 9>/var/lock/eve-sentry-deploy.lock
flock -n 9
case "${1:-}" in
  restore-route)
    if [[ -n "$(ip -4 rule show priority "$table")" ]]; then
      ip -4 rule del "${rule[@]}"
    fi
    if [[ -n "$(ip -4 route show table "$table" exact "$gateway_ip/32" 2>/dev/null || true)" ]]; then
      ip -4 route del "${route[@]}"
    fi
    echo "Temporary ESI routing policy removed"
    ;;
  prefer-old-source)
    baseline=$(ip -4 route get "$gateway_ip" ipproto tcp dport 8787)
    [[ "$baseline" == *"via $rescue_peer dev $rescue_interface src 10.233.54.204 "* ]]
    [[ -z "$(ip -4 rule show table "$table")" && -z "$(ip -4 rule show priority "$table")" ]]
    table_contents=$(ip -4 route show table "$table" 2>&1) || [[ "$table_contents" == *"FIB table does not exist"* ]]
    [[ -z "$table_contents" || "$table_contents" == *"FIB table does not exist"* ]]

    ip -4 route add "${route[@]}"
    rule_added=false
    cleanup() {
      if [[ "$rule_added" == true ]]; then ip -4 rule del "${rule[@]}" || true; fi
      ip -4 route del "${route[@]}" || true
    }
    trap cleanup EXIT
    ip -4 rule add "${rule[@]}"
    rule_added=true
    selected=$(ip -4 route get "$gateway_ip" ipproto tcp dport 8787)
    [[ "$selected" == *" src $source_ip "* ]]

    # Reuse the deployed relay preflight without trying to reacquire the deployment lock.
    /opt/eve-sentry/.venv-server/bin/python - <<'PY'
from pathlib import Path
from runpy import run_path

checks = run_path("/opt/eve-sentry/deploy/ci/configure_esi_transport.py")
try:
    env = checks["environment"](Path("/etc/eve-sentry/eve-sentry.env").read_text())
    if (env.get("EVE_SENTRY_ESI_TRANSPORT") != "relay"
            or env.get("EVE_SENTRY_SERVER_ESI_GATEWAY_URL", "").rstrip("/") != "http://10.233.53.17:8787"):
        raise ValueError("unexpected ESI configuration")
    checks["health"]("http://127.0.0.1:8765/api/readyz")
    checks["preflight"]("server", "relay", env)
except Exception as error:
    raise SystemExit(f"ESI preflight failed ({type(error).__name__})") from None
PY
    trap - EXIT
    echo "ESI relay verified with source $source_ip"
    ;;
  *) exit 2 ;;
esac
