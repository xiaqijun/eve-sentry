#!/usr/bin/env bash
# Package the current commit and deploy it on a Linux host without containers.

set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
deploy_root=${1:-/opt/eve-risk-analysis}
postgres_password=${EVE_RISK_POSTGRES_PASSWORD:?EVE_RISK_POSTGRES_PASSWORD is required}
redis_password=${EVE_RISK_REDIS_PASSWORD:?EVE_RISK_REDIS_PASSWORD is required}
release_sha=$(git -C "$repo_root" rev-parse HEAD)
archive=$(mktemp "${TMPDIR:-/tmp}/eve-risk-analysis-${release_sha}.XXXXXX.tar.gz")
credentials_file=$(mktemp "${TMPDIR:-/tmp}/eve-risk-analysis-credentials.XXXXXX.env")
cleanup() {
    rm -f -- "$archive" "$credentials_file"
}
trap cleanup EXIT

git -C "$repo_root" archive --format=tar.gz --output="$archive" HEAD
chmod 0600 "$credentials_file"
printf 'POSTGRES_PASSWORD=%s\nREDIS_PASSWORD=%s\n' \
    "$postgres_password" "$redis_password" > "$credentials_file"
bash "$repo_root/scripts/ci/deploy_release.sh" \
    "$archive" "$release_sha" "$deploy_root" "$credentials_file"
