#!/usr/bin/env bash
set -Eeuo pipefail

revision="${1:?git revision is required}"
output_dir="${2:-dist}"
repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

[[ "$revision" =~ ^[0-9a-f]{40}$ ]] || { echo "Invalid git revision: $revision" >&2; exit 2; }
git -C "$repository_root" cat-file -e "${revision}^{commit}" 2>/dev/null || { echo "Unknown git revision: $revision" >&2; exit 2; }

case "$output_dir" in
    /*) ;;
    *) output_dir="$repository_root/$output_dir" ;;
esac

work_dir="$(mktemp -d)"
trap 'rm -rf -- "$work_dir"' EXIT
payload="$work_dir/payload"
backend="$payload/backend"
install -d "$backend" "$output_dir"
git -C "$repository_root" archive "$revision" \
    esi_gateway \
    scripts/esi_gateway.py \
    deploy/linux/eve-sentry-esi-gateway.service \
    deploy/postgres/001_esi_id_cache.sql \
    pyproject.toml | tar -x -C "$backend"

cat > "$payload/manifest.json" <<EOF
{
  "artifact": "eve-sentry-esi-gateway",
  "revision": "$revision",
  "layout_version": 2
}
EOF

source_date_epoch="$(git -C "$repository_root" show -s --format=%ct "$revision")"
archive_name="eve-sentry-esi-gateway-${revision}.tar.gz"
archive_path="$output_dir/$archive_name"
tar --sort=name --mtime="@${source_date_epoch}" --owner=0 --group=0 --numeric-owner -czf "$archive_path" -C "$payload" .
(
    cd "$output_dir"
    sha256sum "$archive_name" > "${archive_name}.sha256"
)
echo "ARTIFACT=$archive_path"
echo "CHECKSUM=${archive_path}.sha256"
