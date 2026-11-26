#!/usr/bin/env bash
set -euo pipefail

fail() { printf '%s\n' "$*" >&2; exit 2; }

if [[ $# -ne 2 ]]; then
    fail "Usage: bash $0 <gateway-private-ipv4> <sentry-private-ipv4>"
fi

private_ipv4() {
    local ip=$1 a b c d
    local octet='(0|[1-9][0-9]{0,2})'
    [[ "$ip" =~ ^$octet\.$octet\.$octet\.$octet$ ]] || return 1
    IFS=. read -r a b c d <<< "$ip"
    (( a <= 255 && b <= 255 && c <= 255 && d <= 255 )) || return 1
    (( a == 10 || (a == 172 && b >= 16 && b <= 31) || (a == 192 && b == 168) ))
}

gateway_ip=$1
sentry_ip=$2
private_ipv4 "$gateway_ip" || fail "Gateway IP must be an exact private IPv4 address."
private_ipv4 "$sentry_ip" || fail "Sentry IP must be an exact private IPv4 address."
[[ "$gateway_ip" != "$sentry_ip" ]] || fail "Gateway and Sentry must use distinct addresses."
docker compose version >/dev/null || fail "Install Docker Engine and the Docker Compose plugin."

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
config="$(cd ../.. && pwd)/gateway.env"
if [[ ! -e "$config" ]]; then
    umask 077
    temporary=$(mktemp "${config%/*}/.gateway.env.XXXXXX")
    trap 'rm -f -- "$temporary"' EXIT
    token=$(od -An -N32 -tx1 /dev/urandom | tr -d '[:space:]')
    [[ "$token" =~ ^[0-9a-f]{64}$ ]] || fail "Gateway token generation failed."
    printf '%s\n' \
        "EVE_SENTRY_ESI_GATEWAY_HOST=$gateway_ip" \
        "EVE_SENTRY_ESI_GATEWAY_PORT=8787" \
        "EVE_SENTRY_ESI_GATEWAY_ALLOWED_CLIENTS=$sentry_ip" \
        "EVE_SENTRY_ESI_GATEWAY_TOKEN=$token" > "$temporary"
    ln -- "$temporary" "$config"
fi
[[ "$(sed -n 's/^EVE_SENTRY_ESI_GATEWAY_HOST=//p' "$config")" == "$gateway_ip" &&
   "$(sed -n 's/^EVE_SENTRY_ESI_GATEWAY_ALLOWED_CLIENTS=//p' "$config")" == "$sentry_ip" &&
   "$(sed -n 's/^EVE_SENTRY_ESI_GATEWAY_PORT=//p' "$config")" == 8787 ]] ||
    fail "The existing gateway.env uses different addresses or port."

docker compose up -d --build --wait --wait-timeout 60
printf 'Gateway: http://%s:8787\nToken file: %s\n' "$gateway_ip" "$config"
