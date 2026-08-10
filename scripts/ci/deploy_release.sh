#!/usr/bin/env bash
# Deploy one immutable EVE Risk Analysis release on the production host.

set -euo pipefail

archive=${1:?deployment archive is required}
release_sha=${2:?release SHA is required}
deploy_root=${3:?deployment root is required}
project_name="eve-risk-analysis"

if [[ ! "$release_sha" =~ ^[0-9a-fA-F]{7,64}$ ]]; then
    echo "Invalid release SHA: $release_sha" >&2
    exit 2
fi
case "$deploy_root" in
    /*) ;;
    *)
        echo "Deployment root must be an absolute path." >&2
        exit 2
        ;;
esac
case "$deploy_root" in
    /|/opt|/root|/home)
        echo "Deployment root is too broad: $deploy_root" >&2
        exit 2
        ;;
esac
if [[ ! -f "$archive" ]]; then
    echo "Deployment archive does not exist: $archive" >&2
    exit 2
fi
if [[ ! -f "$deploy_root/.env" ]]; then
    echo "Production environment file is missing: $deploy_root/.env" >&2
    exit 2
fi

mkdir -p "$deploy_root/releases"
exec 9>"$deploy_root/.deploy.lock"
if ! flock -w 900 9; then
    echo "Another deployment still holds the production lock." >&2
    exit 3
fi

release_dir="$deploy_root/releases/$release_sha"
mkdir -p "$release_dir"
tar -xzf "$archive" -C "$release_dir"
rm -f "$archive"

for required in Dockerfile docker-compose.yml pyproject.toml uv.lock; do
    if [[ ! -f "$release_dir/$required" ]]; then
        echo "Release is missing $required" >&2
        exit 4
    fi
done
ln -sfn "$deploy_root/.env" "$release_dir/.env"

previous_dir=""
if [[ -L "$deploy_root/current" ]]; then
    previous_dir=$(readlink -f "$deploy_root/current" || true)
elif [[ -f "$deploy_root/docker-compose.yml" ]]; then
    previous_dir="$deploy_root"
fi

compose() {
    local source_dir=$1
    shift
    docker compose \
        --project-name "$project_name" \
        --env-file "$deploy_root/.env" \
        --file "$source_dir/docker-compose.yml" \
        "$@"
}

wait_until_ready() {
    local source_dir=$1
    local attempt
    for attempt in $(seq 1 30); do
        if compose "$source_dir" ps --status running --services | grep -Fxq bot \
            && compose "$source_dir" ps --status running --services | grep -Fxq worker \
            && compose "$source_dir" exec -T bot python -c \
                'import json, urllib.request; payload=json.load(urllib.request.urlopen("http://127.0.0.1:8080/health/ready", timeout=5)); raise SystemExit(0 if payload.get("status") == "ok" else 1)' \
                >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    return 1
}

start_release() {
    local source_dir=$1
    compose "$source_dir" up -d --build --remove-orphans
    wait_until_ready "$source_dir"
}

if start_release "$release_dir"; then
    ln -sfn "$release_dir" "$deploy_root/current"
    compose "$release_dir" ps
    echo "EVE Risk Analysis deployed successfully: $release_sha"
    exit 0
fi

echo "Deployment health check failed for $release_sha" >&2
compose "$release_dir" logs --no-color --tail 120 bot worker >&2 || true
if [[ -n "$previous_dir" && -f "$previous_dir/docker-compose.yml" ]]; then
    echo "Rolling back to $previous_dir" >&2
    if start_release "$previous_dir"; then
        if [[ "$previous_dir" != "$deploy_root" ]]; then
            ln -sfn "$previous_dir" "$deploy_root/current"
        fi
        echo "Rollback completed." >&2
    else
        echo "Rollback failed; manual recovery is required." >&2
    fi
fi
exit 5
