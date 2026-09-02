#!/usr/bin/env bash
# Deploy one immutable EVE Risk Analysis release without containers.

set -euo pipefail

archive=${1:?deployment archive is required}
release_sha=${2:?release SHA is required}
deploy_root=${3:?deployment root is required}
service_prefix=${SERVICE_PREFIX:-eve-risk-analysis}
data_dir="$deploy_root/data"
unit_dir="$deploy_root/systemd"
runtime_env="$deploy_root/.runtime.env"

docker_env_value() {
    local container_id=$1
    local variable_name=$2
    docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$container_id" \
        | sed -n "s/^${variable_name}=//p" | tail -n 1
}

docker_container_ip() {
    local container_id=$1
    docker inspect --format '{{range .NetworkSettings.Networks}}{{println .IPAddress}}{{end}}' \
        "$container_id" | sed -n '/./{p;q;}'
}

find_eve_risk_postgres_container() {
    local container_id
    while IFS= read -r container_id; do
        if [[ "$(docker_env_value "$container_id" POSTGRES_DB)" == "eve_risk" ]]; then
            printf '%s\n' "$container_id"
            return 0
        fi
    done < <(docker ps --quiet)
    return 1
}

if [[ ! "$release_sha" =~ ^[0-9a-fA-F]{7,64}$ ]]; then
    echo "Invalid release SHA: $release_sha" >&2
    exit 2
fi
case "$deploy_root" in
    /*) ;;
    *) echo "Deployment root must be an absolute path." >&2; exit 2 ;;
esac
case "$deploy_root" in
    /|/opt|/root|/home|/var)
        echo "Deployment root is too broad: $deploy_root" >&2
        exit 2
        ;;
esac
for command in python3 systemctl curl flock tar; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "Required command is missing: $command" >&2
        exit 2
    fi
done
if [[ ! -f "$archive" ]]; then
    echo "Deployment archive does not exist: $archive" >&2
    exit 2
fi
if [[ ! -f "$deploy_root/.env" ]]; then
    echo "Production environment file is missing: $deploy_root/.env" >&2
    exit 2
fi

mkdir -p "$deploy_root/releases" "$data_dir" "$unit_dir"
database_url=$(sed -n 's/^DATABASE_URL=//p' "$deploy_root/.env" | tail -n 1)
database_url=${database_url:-postgresql+asyncpg://eve_risk:eve_risk@127.0.0.1:5432/eve_risk}
database_url=${database_url/@postgres:/@127.0.0.1:}
postgres_password=$(sed -n 's/^POSTGRES_PASSWORD=//p' "$deploy_root/.env" | tail -n 1)
redis_url=$(sed -n 's/^REDIS_URL=//p' "$deploy_root/.env" | tail -n 1)
redis_url=${redis_url:-redis://127.0.0.1:6379/0}
redis_url=${redis_url/redis:\/\/redis:/redis:\/\/127.0.0.1:}
bootstrap_local_postgres=false

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    postgres_container=$(find_eve_risk_postgres_container || true)
    if [[ -n "$postgres_container" ]]; then
        postgres_host=$(docker_container_ip "$postgres_container")
        postgres_user=$(docker_env_value "$postgres_container" POSTGRES_USER)
        postgres_database=$(docker_env_value "$postgres_container" POSTGRES_DB)
        postgres_password=$(docker_env_value "$postgres_container" POSTGRES_PASSWORD)
        postgres_user=${postgres_user:-eve_risk}
        postgres_database=${postgres_database:-eve_risk}
        if [[ -z "$postgres_host" || -z "$postgres_password" ]]; then
            echo "The eve_risk PostgreSQL container is missing its IP address or password." >&2
            exit 2
        fi
        encoded_connection=$(POSTGRES_USER_VALUE="$postgres_user" \
            POSTGRES_PASSWORD_VALUE="$postgres_password" POSTGRES_DATABASE_VALUE="$postgres_database" \
            python3 -c 'import os, urllib.parse; print(":".join((urllib.parse.quote(os.environ["POSTGRES_USER_VALUE"], safe=""), urllib.parse.quote(os.environ["POSTGRES_PASSWORD_VALUE"], safe=""))) + "@" + os.environ["POSTGRES_DATABASE_VALUE"] )')
        encoded_user_password=${encoded_connection%@*}
        encoded_database=${encoded_connection#*@}
        database_url="postgresql+asyncpg://${encoded_user_password}@${postgres_host}:5432/${encoded_database}"

        postgres_project=$(docker inspect --format \
            '{{index .Config.Labels "com.docker.compose.project"}}' "$postgres_container")
        redis_container=""
        if [[ -n "$postgres_project" ]]; then
            redis_container=$(docker ps --quiet \
                --filter "label=com.docker.compose.project=$postgres_project" \
                --filter 'label=com.docker.compose.service=redis' | head -n 1)
        fi
        if [[ -n "$redis_container" ]]; then
            redis_host=$(docker_container_ip "$redis_container")
            if [[ -z "$redis_host" ]]; then
                echo "The Redis container is missing its bridge IP address." >&2
                exit 2
            fi
            redis_url="redis://${redis_host}:6379/0"
        fi
        echo "Using the existing Docker PostgreSQL and Redis infrastructure."
    fi
fi

if [[ -z "${postgres_container:-}" && -n "$(getent passwd postgres || true)" ]]; then
    bootstrap_local_postgres=true
    if [[ -z "$postgres_password" ]]; then
        postgres_password=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
        printf '\nPOSTGRES_PASSWORD=%s\n' "$postgres_password" >> "$deploy_root/.env"
        chmod 0600 "$deploy_root/.env"
        echo "Generated and stored a PostgreSQL password in $deploy_root/.env"
    fi
    encoded_password=$(POSTGRES_PASSWORD_VALUE="$postgres_password" python3 -c \
        'import os, urllib.parse; print(urllib.parse.quote(os.environ["POSTGRES_PASSWORD_VALUE"], safe=""))')
    database_url="postgresql+asyncpg://eve_risk:${encoded_password}@127.0.0.1:5432/eve_risk"
fi
sed -e '/^DATABASE_URL=/d' -e '/^REDIS_URL=/d' "$deploy_root/.env" > "$runtime_env"
printf 'DATABASE_URL=%s\nREDIS_URL=%s\n' "$database_url" "$redis_url" >> "$runtime_env"
chmod 0600 "$runtime_env"
uv_bin=$(command -v uv || true)
exec 9>"$deploy_root/.deploy.lock"
if ! flock -w 900 9; then
    echo "Another deployment still holds the production lock." >&2
    exit 3
fi

release_dir="$deploy_root/releases/$release_sha"
case "$release_dir" in
    "$deploy_root/releases/"*) ;;
    *) echo "Invalid release path." >&2; exit 2 ;;
esac
rm -rf -- "$release_dir"
mkdir -p "$release_dir"
tar -xzf "$archive" -C "$release_dir"
rm -f -- "$archive"

for required in pyproject.toml uv.lock README.md alembic.ini; do
    if [[ ! -f "$release_dir/$required" ]]; then
        echo "Release is missing $required" >&2
        exit 4
    fi
done
ln -sfn "$deploy_root/.env" "$release_dir/.env"

if [[ -L "$deploy_root/current" ]]; then
    previous_dir=$(readlink -f "$deploy_root/current" || true)
else
    previous_dir=""
fi

systemctl_cmd() {
    if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
        systemctl "$@"
    else
        systemctl --user "$@"
    fi
}

unit_path() {
    printf '%s/%s.service\n' "$unit_dir" "$1"
}

write_units() {
    local source_dir=$1
    local bot_unit worker_unit
    bot_unit=$(unit_path "$service_prefix-bot")
    worker_unit=$(unit_path "$service_prefix-worker")
    cat > "$bot_unit" <<EOF
[Unit]
Description=EVE Risk Analysis QQ bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$source_dir
EnvironmentFile=-$runtime_env
Environment=SDE_INDEX_PATH=$data_dir/sde.sqlite3
Environment=PYTHONUNBUFFERED=1
ExecStart=$source_dir/.venv/bin/python -m eve_risk.bot
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
EOF
    cat > "$worker_unit" <<EOF
[Unit]
Description=EVE Risk Analysis worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$source_dir
EnvironmentFile=-$runtime_env
Environment=SDE_INDEX_PATH=$data_dir/sde.sqlite3
Environment=PYTHONUNBUFFERED=1
ExecStart=$source_dir/.venv/bin/arq eve_risk.worker.WorkerSettings
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
EOF
    if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
        install -m 0644 "$bot_unit" "/etc/systemd/system/$service_prefix-bot.service"
        install -m 0644 "$worker_unit" "/etc/systemd/system/$service_prefix-worker.service"
    else
        systemctl_cmd link "$bot_unit" "$worker_unit"
    fi
    systemctl_cmd daemon-reload
}

ensure_postgres() {
    local source_dir=$1
    if [[ "$bootstrap_local_postgres" != true ]]; then
        return 0
    fi
    if [[ "$postgres_password" == *$'\n'* || "$postgres_password" == *$'\r'* ]]; then
        echo "POSTGRES_PASSWORD must not contain line breaks." >&2
        return 1
    fi
    if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
        echo "Non-root deployment expects the eve_risk PostgreSQL role and database to exist." >&2
        return 0
    fi
    if ! command -v runuser >/dev/null 2>&1; then
        echo "Required command is missing: runuser" >&2
        return 1
    fi
    POSTGRES_PASSWORD_VALUE="$postgres_password" runuser -u postgres -- \
        "$source_dir/.venv/bin/python" - <<'PY'
import asyncio
import os

import asyncpg


async def main() -> None:
    connection = await asyncpg.connect(user="postgres", database="postgres")
    try:
        role_exists = await connection.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'eve_risk')"
        )
        if not role_exists:
            await connection.execute("CREATE ROLE eve_risk LOGIN")
        password_literal = await connection.fetchval(
            "SELECT quote_literal($1)", os.environ["POSTGRES_PASSWORD_VALUE"]
        )
        await connection.execute(
            f"ALTER ROLE eve_risk WITH LOGIN PASSWORD {password_literal}"
        )
        database_exists = await connection.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'eve_risk')"
        )
        if not database_exists:
            await connection.execute("CREATE DATABASE eve_risk OWNER eve_risk")
    finally:
        await connection.close()


asyncio.run(main())
PY
}

run_release_setup() {
    local source_dir=$1
    if [[ -n "$uv_bin" ]]; then
        (cd "$source_dir" && "$uv_bin" sync --frozen --no-dev) || return 1
    else
        echo "uv is not installed; creating a Python venv with pip" >&2
        python3 -m venv "$source_dir/.venv" || return 1
        (cd "$source_dir" && .venv/bin/python -m pip install --disable-pip-version-check \
            --no-cache-dir --index-url "${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}" \
            --upgrade pip) || return 1
        (cd "$source_dir" && .venv/bin/python -m pip install --disable-pip-version-check \
            --no-cache-dir --index-url "${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}" .) \
            || return 1
    fi
    ensure_postgres "$source_dir" || return 1
    (cd "$source_dir" && env DATABASE_URL="$database_url" REDIS_URL="$redis_url" \
        .venv/bin/alembic upgrade head) || return 1
    (cd "$source_dir" && env DATABASE_URL="$database_url" REDIS_URL="$redis_url" \
        SDE_INDEX_PATH="$data_dir/sde.sqlite3" .venv/bin/python -m eve_risk.sde) || return 1
}

health_port=$(awk -F= '$1 == "HEALTH_PORT" {gsub(/[[:space:]]/, "", $2); print $2; exit}' "$deploy_root/.env")
health_port=${health_port:-8080}
if [[ ! "$health_port" =~ ^[0-9]+$ ]]; then
    echo "HEALTH_PORT must be numeric: $health_port" >&2
    exit 2
fi

activate_release() {
    local source_dir=$1
    write_units "$source_dir"
    systemctl_cmd enable "$service_prefix-bot.service" "$service_prefix-worker.service"
    systemctl_cmd restart "$service_prefix-bot.service" "$service_prefix-worker.service"
}

wait_until_ready() {
    local attempt
    for attempt in $(seq 1 30); do
        if systemctl_cmd is-active --quiet "$service_prefix-bot.service" \
            && systemctl_cmd is-active --quiet "$service_prefix-worker.service"; then
            health_payload=$(curl --fail --silent \
                "http://127.0.0.1:${health_port}/health/ready" || true)
            if [[ -n "$health_payload" ]] && printf '%s' "$health_payload" \
                | python3 -c 'import json, sys; raise SystemExit(0 if json.load(sys.stdin).get("status") == "ok" else 1)'; then
                return 0
            fi
        fi
        sleep 2
    done
    return 1
}

if run_release_setup "$release_dir" && activate_release "$release_dir" \
    && wait_until_ready; then
    ln -sfn "$release_dir" "$deploy_root/current"
    systemctl_cmd --no-pager --full status \
        "$service_prefix-bot.service" "$service_prefix-worker.service" || true
    echo "EVE Risk Analysis deployed successfully: $release_sha"
    exit 0
fi

echo "Deployment health check failed for $release_sha" >&2
systemctl_cmd --no-pager --full status \
    "$service_prefix-bot.service" "$service_prefix-worker.service" >&2 || true
if [[ -n "$previous_dir" && -d "$previous_dir/.venv" ]]; then
    echo "Rolling back to $previous_dir" >&2
    if activate_release "$previous_dir" && wait_until_ready; then
        ln -sfn "$previous_dir" "$deploy_root/current"
        echo "Rollback completed." >&2
    else
        echo "Rollback failed; manual recovery is required." >&2
    fi
fi
exit 5
