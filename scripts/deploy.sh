#!/usr/bin/env bash
# Package the current commit and deploy it on a Linux host without containers.

set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
deploy_root=${1:-/opt/eve-risk-analysis}
release_sha=$(git -C "$repo_root" rev-parse HEAD)
archive=$(mktemp "${TMPDIR:-/tmp}/eve-risk-analysis-${release_sha}.XXXXXX.tar.gz")
cleanup() {
    rm -f -- "$archive"
}
trap cleanup EXIT

git -C "$repo_root" archive --format=tar.gz --output="$archive" HEAD
bash "$repo_root/scripts/ci/deploy_release.sh" "$archive" "$release_sha" "$deploy_root"
